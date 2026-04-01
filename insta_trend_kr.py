#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
insta_trend_kr.py — 한국 SNS 트렌드 키워드 수집 v5
매일 오전 10시 텔레그램 전송

데이터 소스:
  1. Google Trends RSS      — 한국 실시간 검색 트렌드 (geo=KR)
  2. Naver DataLab 쇼핑      — 쇼핑 인기검색어 (패션/뷰티/리빙)
  3. Naver signal.bz         — 실시간 급상승 검색어
  4. X(Twitter) 한국 트렌드   — 트위터 한국 트렌딩 토픽 (ZUM 포털 경유)
  5. 커뮤니티 (더쿠/네이트판)  — 실시간 인기 키워드
  6. Naver 검색 연관 해시태그  — 인스타 관련 태그
  7. Naver 블로그 인기글      — 라이프스타일 키워드
  8. 틱톡 트렌드 (간접)       — 챌린지/바이럴 키워드
"""

import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import requests

# ──────────────────────────────────────────────
# 상수 & 경로
# ──────────────────────────────────────────────
HOME = Path.home()
STATE_DIR = HOME / ".openclaw/state"
SCORE_FILE = STATE_DIR / "insta_trends.jsonl"

GOOGLE_TRENDS_RSS = "https://trends.google.com/trending/rss?geo=KR&hl=ko"

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8356417720:AAHo1oH_NiY5gImMOGvbHXewPkZkdBFjJlQ"
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8687945199")
TELEGRAM_MAX_CHARS = 4096

TOP_N = 25  # 최종 출력 해시태그 수

# 네이버 검색에서 인스타 연관 태그를 뽑을 시드 쿼리들
NAVER_SEED_QUERIES = [
    "인스타그램 인기 해시태그",
    "인스타 오늘코디 OOTD",
    "인스타 맛집 추천",
    "인스타 뷰티 메이크업",
    "인스타 여행 핫플",
    "인스타 카페 추천",
    "틱톡 챌린지",
]

# 카테고리 키워드 매핑
CATEGORIES = {
    "패션/뷰티": [
        "코디", "패션", "뷰티", "메이크업", "화장", "스킨케어", "헤어",
        "네일", "옷", "슈즈", "가방", "악세사리", "스타일", "룩북",
        "OOTD", "데일리룩", "직장인룩", "봄코디", "여름코디", "가을코디", "겨울코디",
    ],
    "맛집/카페": [
        "맛집", "카페", "디저트", "브런치", "먹스타", "맛스타", "베이커리",
        "레시피", "홈카페", "음식", "요리", "먹방",
    ],
    "여행/핫플": [
        "여행", "핫플", "성수", "한남", "을지로", "연남", "익선동",
        "제주", "부산", "강릉", "속초", "전시", "팝업",
    ],
    "라이프": [
        "일상", "데일리", "집꾸미기", "인테리어", "반려", "운동",
        "필라테스", "요가", "헬스", "다이어트", "자기계발",
    ],
    "엔터": [
        "아이돌", "케이팝", "드라마", "영화", "예능", "콘서트", "컴백",
        "팬미팅", "뮤직비디오", "음원", "차트",
    ],
}

# 불용어: 해시태그로 쓰기 어색한 단어 제거
STOPWORDS = {
    "인스타그램", "인스타", "해시태그", "오늘", "내일", "이번", "지난",
    "관련", "검색", "최근", "많은", "있다", "없다", "하다", "되다",
    "이다", "때문", "그리고", "그래서", "하지만", "저는", "우리",
    "트렌드", "인기", "급상승", "검색어", "뉴스", "기사", "속보",
    "종합", "사진", "영상", "기자", "앵커", "보도", "발표",
    "연합뉴스", "한겨레", "조선일보", "중앙일보", "동아일보",
}

# 요일 한국어
WEEKDAY_KO = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

# 공통 브라우저 헤더
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.3",
}


# ──────────────────────────────────────────────
# 유틸리티
# ──────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    lines = message.split("\n")
    chunks: List[str] = []
    current = ""
    for line in lines:
        candidate = current + "\n" + line if current else line
        if len(candidate) > TELEGRAM_MAX_CHARS:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    success = True
    for i, chunk in enumerate(chunks):
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if not resp.ok:
                log(f"텔레그램 전송 실패 (chunk {i+1}): {resp.status_code} {resp.text[:200]}")
                success = False
        except Exception as e:
            log(f"텔레그램 예외: {e}")
            success = False
        if i < len(chunks) - 1:
            time.sleep(0.5)
    return success


def classify_keyword(keyword: str) -> str:
    """키워드를 카테고리로 분류"""
    kw_lower = keyword.lower()
    for category, terms in CATEGORIES.items():
        for term in terms:
            if term.lower() in kw_lower:
                return category
    return "이슈/화제"


def load_previous_tags() -> set:
    """이전 수집 결과에서 태그 목록 로드 (신규 트렌드 감지용)"""
    try:
        if not SCORE_FILE.exists():
            return set()
        with SCORE_FILE.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if not lines:
            return set()
        last = json.loads(lines[-1])
        return {item["tag"] for item in last.get("tags", [])}
    except Exception:
        return set()


# ──────────────────────────────────────────────
# 소스 1: Google Trends RSS — 한국 실시간 트렌드
# ──────────────────────────────────────────────

def fetch_google_trends() -> Tuple[List[str], List[str]]:
    """
    구글 트렌드 한국 RSS에서 실시간 검색 키워드 수집.
    반환: (한글 태그 리스트, 트렌드 항목 표시용 문자열 리스트)
    """
    try:
        resp = requests.get(GOOGLE_TRENDS_RSS, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        ns = {"ht": "https://trends.google.com/trending/rss"}
        items = root.findall(".//item")

        tags: List[str] = []
        display: List[str] = []

        for item in items:
            title = item.find("title")
            traffic = item.find("ht:approx_traffic", ns)
            if title is None:
                continue
            keyword = title.text.strip()
            traffic_txt = traffic.text.strip() if traffic is not None else ""

            if re.search(r"[가-힣]", keyword):
                clean = re.sub(r"\s+", "", keyword)
                if clean not in STOPWORDS and len(clean) >= 2:
                    tags.append(f"#{clean}")

            suffix = f" ({traffic_txt})" if traffic_txt else ""
            display.append(f"{keyword}{suffix}")

        log(f"[Google Trends] 키워드 {len(display)}개, 태그 {len(tags)}개")
        return tags, display[:10]
    except Exception as e:
        log(f"[Google Trends 실패] {e}")
        return [], []


# ──────────────────────────────────────────────
# 소스 2: Naver DataLab 쇼핑 인사이트
# ──────────────────────────────────────────────

def fetch_naver_datalab_shopping() -> List[str]:
    """
    네이버 데이터랩 쇼핑인사이트 인기검색어 페이지 스크래핑.
    패션/뷰티 카테고리 인기 키워드 수집.
    """
    tags: List[str] = []
    try:
        url = "https://datalab.naver.com/shoppingInsight/sCategory.naver"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            keywords = re.findall(r'"keyword"\s*:\s*"([가-힣a-zA-Z0-9\s]{2,20})"', resp.text)
            for kw in keywords:
                clean = kw.strip()
                if clean and clean not in STOPWORDS and re.search(r"[가-힣]", clean):
                    tag = re.sub(r"\s+", "", clean)
                    tags.append(f"#{tag}")
            log(f"[Naver DataLab 쇼핑] {len(tags)}개 수집")
    except Exception as e:
        log(f"[Naver DataLab 쇼핑 실패] {e}")

    # 추가: 네이버 쇼핑 실시간 인기 검색어
    try:
        url2 = "https://shopping.naver.com/api/modules/gnb/popular-searches"
        resp2 = requests.get(url2, headers={**HEADERS, "Referer": "https://shopping.naver.com/"}, timeout=10)
        if resp2.status_code == 200:
            data = resp2.json()
            items = data if isinstance(data, list) else data.get("items", data.get("keywords", []))
            for item in items[:20]:
                kw = item.get("keyword", item.get("text", "")) if isinstance(item, dict) else str(item)
                if kw and re.search(r"[가-힣]", kw):
                    tag = re.sub(r"\s+", "", kw)
                    if tag not in STOPWORDS:
                        tags.append(f"#{tag}")
            log(f"[Naver 쇼핑 인기] 추가 수집 완료, 총 {len(tags)}개")
    except Exception as e:
        log(f"[Naver 쇼핑 인기 실패] {e}")

    return tags


# ──────────────────────────────────────────────
# 소스 3: Naver signal.bz 실시간 급상승 검색어
# ──────────────────────────────────────────────

def fetch_naver_realtime() -> List[str]:
    """signal.bz 비공식 API로 네이버 실시간 급상승 검색어 수집"""
    try:
        resp = requests.get(
            "https://api.signal.bz/news/realtime",
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        keywords: List[str] = []
        if isinstance(data, dict) and "top10" in data:
            for item in data["top10"][:20]:
                kw = item.get("keyword", "")
                if kw:
                    keywords.append(kw)
        elif isinstance(data, list):
            for item in data[:20]:
                kw = (item.get("keyword") or item.get("title") or "") if isinstance(item, dict) else str(item)
                if kw:
                    keywords.append(kw)

        korean_only = [kw for kw in keywords if re.search(r"[가-힣]", kw)]
        log(f"[Naver signal.bz] {len(korean_only)}개 수집")
        return korean_only
    except Exception as e:
        log(f"[Naver signal.bz 실패] {e}")
        return []


# ──────────────────────────────────────────────
# 소스 4: X(Twitter) 한국 트렌딩
# ──────────────────────────────────────────────

def fetch_x_trends() -> Tuple[List[str], List[str]]:
    """
    X(Twitter) 한국 트렌딩 + 포털 실시간 키워드 수집.
    반환: (해시태그 리스트, 표시용 문자열 리스트)
    """
    tags: List[str] = []
    display: List[str] = []

    # 방법 1: ZUM 포털 실시간 키워드 (트위터 + 커뮤니티 반영됨)
    try:
        resp = requests.get("https://zum.com/", headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            kws = re.findall(r'keyword[^>]*>([가-힣][^<]*)<', resp.text)
            if not kws:
                kws = re.findall(r'issue[^>]*>([가-힣][^<]{1,30})<', resp.text)
            for kw in kws[:20]:
                kw = kw.strip()
                if re.search(r"[가-힣]", kw) and len(kw) >= 2:
                    words = re.findall(r"[가-힣]{2,8}", kw)
                    for w in words:
                        if w not in STOPWORDS:
                            tags.append(f"#{w}")
                    display.append(kw)
            log(f"[ZUM 실시간] {len(tags)}개 수집")
    except Exception as e:
        log(f"[ZUM 실시간 실패] {e}")

    # 방법 2: 네이버 검색으로 트위터 트렌드 간접 수집
    try:
        resp = requests.get(
            "https://search.naver.com/search.naver",
            headers=HEADERS,
            params={"query": "트위터 실시간 트렌드 한국", "where": "nexearch"},
            timeout=10,
        )
        if resp.status_code == 200:
            hash_tags = re.findall(r"#([가-힣]{2,10})", resp.text)
            for t in hash_tags[:10]:
                if t not in STOPWORDS:
                    tags.append(f"#{t}")
            log(f"[Naver→X 간접] {len(hash_tags)}개 추가 수집")
    except Exception as e:
        log(f"[Naver→X 간접 실패] {e}")

    return tags, display


# ──────────────────────────────────────────────
# 소스 5: 커뮤니티 실시간 인기 키워드 (더쿠/인스티즈)
# ──────────────────────────────────────────────

def fetch_community_trends() -> List[str]:
    """
    한국 커뮤니티 사이트에서 실시간 인기 키워드 수집.
    더쿠(theqoo), 인스티즈(instiz) 실시간 검색어/인기글 제목 분석.
    """
    tags: List[str] = []

    # 더쿠 실시간 인기글
    try:
        resp = requests.get(
            "https://theqoo.net/hot",
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            titles = re.findall(r'class="title"[^>]*>\s*<a[^>]*>([^<]+)</a>', resp.text)
            if not titles:
                titles = re.findall(r'document_srl=[^"]*"[^>]*>([^<]+)</a>', resp.text)
            for title in titles[:15]:
                words = re.findall(r"[가-힣]{2,8}", title)
                for w in words:
                    if w not in STOPWORDS:
                        tags.append(f"#{w}")
            log(f"[더쿠] {len(tags)}개 키워드 추출")
    except Exception as e:
        log(f"[더쿠 실패] {e}")

    # 네이트판 실시간 인기글
    prev_count = len(tags)
    try:
        resp = requests.get(
            "https://pann.nate.com/talk/ranking",
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            titles = re.findall(r'class="tit"[^>]*>([^<]+)', resp.text)
            if not titles:
                titles = re.findall(r'<h2[^>]*>([^<]+)</h2>', resp.text)
            for title in titles[:15]:
                words = re.findall(r"[가-힣]{2,8}", title)
                for w in words:
                    if w not in STOPWORDS:
                        tags.append(f"#{w}")
            log(f"[네이트판] {len(tags) - prev_count}개 키워드 추출")
    except Exception as e:
        log(f"[네이트판 실패] {e}")

    return tags


# ──────────────────────────────────────────────
# 소스 6: Naver 검색 연관 해시태그
# ──────────────────────────────────────────────

def fetch_naver_related_tags(query: str) -> List[str]:
    """네이버 검색 결과 HTML에서 인스타 연관 해시태그 추출"""
    try:
        url = "https://search.naver.com/search.naver"
        params = {"query": query, "where": "nexearch"}
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if resp.status_code != 200:
            return []

        html = resp.text
        tags = re.findall(r"#([가-힣]{2,10})", html)
        tags += re.findall(r'data-tag="([가-힣]{2,10})"', html)
        tags += re.findall(r'class="tag[^"]*">([가-힣]{2,10})', html)

        seen: set = set()
        result = []
        for t in tags:
            if t not in seen and t not in STOPWORDS:
                seen.add(t)
                result.append(f"#{t}")
        return result[:15]
    except Exception as e:
        log(f"[Naver 연관태그 실패] {query}: {e}")
        return []


def fetch_all_naver_tags() -> List[str]:
    all_tags: List[str] = []
    for query in NAVER_SEED_QUERIES:
        tags = fetch_naver_related_tags(query)
        all_tags.extend(tags)
        time.sleep(0.4)
    log(f"[Naver 연관태그] 총 {len(all_tags)}개 수집")
    return all_tags


# ──────────────────────────────────────────────
# 소스 7: Naver 블로그 인기글 키워드
# ──────────────────────────────────────────────

def fetch_naver_blog_trends() -> List[str]:
    """네이버 블로그 인기글 제목에서 라이프스타일 키워드 추출"""
    tags: List[str] = []
    try:
        resp = requests.get(
            "https://section.blog.naver.com/ajax/DirectoryPostList.naver"
            "?directoryNo=0&currentPage=1&countPerPage=20",
            headers={**HEADERS, "Referer": "https://section.blog.naver.com/"},
            timeout=10,
        )
        if resp.status_code == 200:
            # 응답이 )]}' 프리픽스로 시작할 수 있으므로 제거 후 JSON 파싱
            text = resp.text
            if text.startswith(")]}'"):
                text = text[4:].strip()
            try:
                data = json.loads(text)
                post_list = data.get("result", {}).get("postList", [])
                for post in post_list[:20]:
                    title = post.get("title", "")
                    words = re.findall(r"[가-힣]{2,8}", title)
                    for w in words:
                        if w not in STOPWORDS:
                            tags.append(f"#{w}")
            except (json.JSONDecodeError, AttributeError):
                # JSON 파싱 실패 시 정규식 폴백
                titles = re.findall(r'"title"\s*:\s*"([^"]+)"', resp.text)
                for title in titles[:20]:
                    words = re.findall(r"[가-힣]{2,8}", title)
                    for w in words:
                        if w not in STOPWORDS:
                            tags.append(f"#{w}")
            log(f"[Naver 블로그] {len(tags)}개 키워드 추출")
    except Exception as e:
        log(f"[Naver 블로그 실패] {e}")
    return tags


# ──────────────────────────────────────────────
# 소스 8: 틱톡 트렌드 (네이버 검색 간접)
# ──────────────────────────────────────────────

def fetch_tiktok_trends() -> List[str]:
    """네이버 검색을 통해 틱톡 인기 챌린지/트렌드 키워드 간접 수집"""
    tags: List[str] = []
    queries = [
        "틱톡 챌린지 인기",
        "틱톡 트렌드 오늘",
        "숏폼 인기 영상",
    ]
    for query in queries:
        try:
            resp = requests.get(
                "https://search.naver.com/search.naver",
                headers=HEADERS,
                params={"query": query, "where": "nexearch"},
                timeout=10,
            )
            if resp.status_code == 200:
                found = re.findall(r"#([가-힣]{2,10})", resp.text)
                for t in found:
                    if t not in STOPWORDS:
                        tags.append(f"#{t}")
            time.sleep(0.3)
        except Exception as e:
            log(f"[틱톡 간접 실패] {query}: {e}")
    # 중복 제거
    seen: set = set()
    unique: List[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    log(f"[틱톡 트렌드] {len(unique)}개 키워드 수집")
    return unique


# ──────────────────────────────────────────────
# 집계 & 랭킹
# ──────────────────────────────────────────────

def rank_tags(
    naver_realtime: List[str],
    naver_related: List[str],
    google_tags: List[str],
    x_tags: List[str],
    community_tags: List[str],
    shopping_tags: List[str],
    blog_tags: List[str],
    tiktok_tags: List[str],
) -> List[Tuple[str, int]]:
    """
    모든 소스에서 수집된 태그를 가중치 기반으로 집계.
    SNS 소스 (X/트위터, 틱톡, 커뮤니티)에 높은 가중치 부여.
    여러 소스에 동시 등장 시 보너스 점수.
    """
    counter: Counter = Counter()
    source_tracker: Dict[str, set] = {}  # 태그별 등장 소스 추적

    def add_tags(tag_list: List[str], weight: int, source_name: str):
        for raw in tag_list:
            tag = f"#{raw}" if not raw.startswith("#") else raw
            clean = tag.lstrip("#")
            if clean in STOPWORDS or not re.search(r"[가-힣]", clean) or len(clean) < 2:
                continue
            counter[tag] += weight
            source_tracker.setdefault(tag, set()).add(source_name)

    # 가중치: SNS > 커뮤니티 > 검색트렌드 > 블로그/쇼핑
    add_tags(x_tags, 4, "X/트위터")
    add_tags(tiktok_tags, 4, "틱톡")
    add_tags(community_tags, 3, "커뮤니티")
    add_tags(naver_realtime, 3, "네이버실시간")
    add_tags(google_tags, 2, "구글트렌드")
    add_tags(naver_related, 2, "네이버연관")
    add_tags(blog_tags, 2, "블로그")
    add_tags(shopping_tags, 1, "쇼핑")

    # 다중 소스 보너스: 2개 이상 소스에 등장하면 보너스
    for tag, sources in source_tracker.items():
        if len(sources) >= 3:
            counter[tag] += 5  # 3개 이상 소스
        elif len(sources) >= 2:
            counter[tag] += 2  # 2개 소스

    return counter.most_common(TOP_N)


# ──────────────────────────────────────────────
# 메시지 포맷
# ──────────────────────────────────────────────

def build_message(
    top_tags: List[Tuple[str, int]],
    naver_realtime: List[str],
    google_trends: List[str],
    x_display: List[str],
    previous_tags: set,
    sources_ok: List[str],
) -> str:
    """텔레그램 HTML 메시지 생성 — 카테고리 분류 + 신규 트렌드 표시"""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    weekday_ko = WEEKDAY_KO[now.weekday()]
    hour = now.hour
    ampm = "오전" if hour < 12 else "오후"
    h12 = hour if hour <= 12 else hour - 12
    time_str = f"{ampm} {h12:02d}:{now.strftime('%M')}"

    lines = [
        "📱 <b>한국 SNS 트렌드 리포트</b>",
        f"📅 {date_str} ({weekday_ko}) {time_str}",
        "",
    ]

    # ── TOP 25 해시태그 ──
    lines.append("🔥 <b>오늘의 인기 해시태그 TOP 25</b>")
    if top_tags:
        new_count = 0
        for i, (tag, score) in enumerate(top_tags, 1):
            is_new = tag not in previous_tags
            new_badge = " 🆕" if is_new else ""
            if is_new:
                new_count += 1
            category = classify_keyword(tag.lstrip("#"))
            cat_label = f"[{category}]" if category != "이슈/화제" else ""
            lines.append(
                f"{i}. {html_escape(tag)}  "
                f"<i>({score}점{' · ' + cat_label if cat_label else ''})</i>"
                f"{new_badge}"
            )
        if new_count:
            lines.append(f"\n🆕 신규 트렌드 {new_count}개 감지!")
    else:
        lines.append("• 수집된 태그 없음")

    # ── 카테고리별 분류 ──
    if top_tags:
        lines.append("")
        lines.append("📂 <b>카테고리별 분류</b>")
        cat_groups: Dict[str, List[str]] = {}
        for tag, _ in top_tags:
            cat = classify_keyword(tag.lstrip("#"))
            cat_groups.setdefault(cat, []).append(tag)

        cat_emojis = {
            "패션/뷰티": "👗", "맛집/카페": "🍽", "여행/핫플": "✈️",
            "라이프": "🏠", "엔터": "🎬", "이슈/화제": "💬",
        }
        for cat, cat_tags in cat_groups.items():
            emoji = cat_emojis.get(cat, "•")
            lines.append(f"{emoji} <b>{cat}:</b> {' '.join(cat_tags[:5])}")

    # ── X(트위터) 트렌딩 ──
    if x_display:
        lines.append("")
        lines.append("🐦 <b>X(트위터) 한국 트렌딩</b>")
        lines.append("• " + " / ".join(x_display[:10]))

    # ── 네이버 실시간 ──
    lines.append("")
    lines.append("📈 <b>네이버 실시간 급상승</b>")
    if naver_realtime:
        kr_only = [kw for kw in naver_realtime if re.search(r"[가-힣]", kw)]
        lines.append("• " + " / ".join(kr_only[:10]))
    else:
        lines.append("• 데이터 없음")

    # ── 구글 트렌드 ──
    lines.append("")
    lines.append("🔍 <b>구글 실시간 트렌드 (한국)</b>")
    if google_trends:
        lines.append("• " + " / ".join(html_escape(item) for item in google_trends[:10]))
    else:
        lines.append("• 데이터 없음")

    # ── 푸터 ──
    lines.append("")
    lines.append("━━━━━━━━━━━━━")
    lines.append(f"📡 수집 소스: {' · '.join(sources_ok) if sources_ok else '없음'}")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# 상태 저장
# ──────────────────────────────────────────────

def save_state(top_tags: List[Tuple[str, int]], sources: List[str]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().isoformat(),
            "tags": [{"tag": tag, "score": score} for tag, score in top_tags],
            "sources": sources,
        }
        with SCORE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"[상태 저장 실패] {e}")


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def main() -> None:
    log("=== 한국 SNS 트렌드 수집 시작 ===")
    sources_ok: List[str] = []

    # 이전 데이터 로드 (신규 트렌드 감지용)
    previous_tags = load_previous_tags()

    # ── 소스 1: Google Trends ──
    google_tags, google_trends = fetch_google_trends()
    if google_trends:
        sources_ok.append("Google Trends")

    # ── 소스 2: Naver DataLab 쇼핑 ──
    shopping_tags = fetch_naver_datalab_shopping()
    if shopping_tags:
        sources_ok.append("Naver 쇼핑")

    # ── 소스 3: Naver 실시간 급상승 ──
    naver_realtime = fetch_naver_realtime()
    if naver_realtime:
        sources_ok.append("Naver 실시간")

    # ── 소스 4: X(Twitter) 트렌딩 ──
    x_tags, x_display = fetch_x_trends()
    if x_tags:
        sources_ok.append("X/트위터")

    # ── 소스 5: 커뮤니티 트렌드 ──
    community_tags = fetch_community_trends()
    if community_tags:
        sources_ok.append("커뮤니티")

    # ── 소스 6: Naver 연관 해시태그 ──
    naver_related = fetch_all_naver_tags()
    if naver_related:
        sources_ok.append("Naver 연관태그")

    # ── 소스 7: Naver 블로그 인기글 ──
    blog_tags = fetch_naver_blog_trends()
    if blog_tags:
        sources_ok.append("블로그")

    # ── 소스 8: 틱톡 트렌드 ──
    tiktok_tags = fetch_tiktok_trends()
    if tiktok_tags:
        sources_ok.append("틱톡")

    # ── 모든 소스 실패 ──
    if not sources_ok:
        send_telegram(
            "⚠️ <b>SNS 트렌드 수집 실패</b>\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            "모든 데이터 소스 수집 실패. 네트워크를 확인해 주세요."
        )
        sys.exit(1)

    # ── 집계 & 랭킹 ──
    top_tags = rank_tags(
        naver_realtime, naver_related, google_tags,
        x_tags, community_tags, shopping_tags,
        blog_tags, tiktok_tags,
    )

    # ── 메시지 전송 ──
    msg = build_message(
        top_tags, naver_realtime, google_trends,
        x_display, previous_tags, sources_ok,
    )
    ok = send_telegram(msg)
    log(f"텔레그램 전송: {'성공' if ok else '실패'}")

    if ok:
        save_state(top_tags, sources_ok)

    log(f"=== 완료 | 소스: {', '.join(sources_ok)} | 태그: {len(top_tags)}개 ===")


if __name__ == "__main__":
    main()
