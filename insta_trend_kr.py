#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
insta_trend_kr.py — 한국 인스타그램 한글 해시태그 트렌드 수집 v4
매일 오전 10시 텔레그램 전송

데이터 소스:
  1. Naver signal.bz   — 실시간 급상승 검색어 → 한글 해시태그
  2. Naver 검색 연관태그  — 인스타 해시태그 검색 결과에서 관련 태그 추출
  3. Google Trends RSS  — 한국 실시간 검색 트렌드 키워드 (geo=KR)
"""

import json
import os
import re
import subprocess
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
HOME            = Path.home()
TAVILY_KEY_PATH = HOME / ".openclaw/credentials/tavily_api_key"
TAVILY_SCRIPT   = HOME / ".openclaw/workspace/skills/tavily/scripts/search.mjs"
STATE_DIR       = HOME / ".openclaw/state"
SCORE_FILE      = STATE_DIR / "insta_trends.jsonl"

GOOGLE_TRENDS_RSS = "https://trends.google.com/trending/rss?geo=KR&hl=ko"

TELEGRAM_BOT_TOKEN = "8356417720:AAHo1oH_NiY5gImMOGvbHXewPkZkdBFjJlQ"
TELEGRAM_CHAT_ID   = "8687945199"
TELEGRAM_MAX_CHARS = 4096

TOP_N = 20  # 최종 출력 해시태그 수

# 네이버 검색에서 인스타 연관 태그를 뽑을 시드 쿼리들
NAVER_SEED_QUERIES = [
    "인스타그램 해시태그",
    "인스타 오늘코디",
    "인스타 맛집",
    "인스타 뷰티",
    "인스타 여행",
]

# 불용어: 해시태그로 쓰기 어색한 단어 제거
STOPWORDS = {
    "인스타그램", "인스타", "해시태그", "오늘", "내일", "이번", "지난",
    "관련", "검색", "최근", "많은", "있다", "없다", "하다", "되다",
    "이다", "때문", "그리고", "그래서", "하지만", "저는", "우리",
    "트렌드", "인기", "급상승", "검색어", "뉴스", "기사",
}

# 요일 한국어
WEEKDAY_KO = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

# 공통 브라우저 헤더
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}


# ──────────────────────────────────────────────
# 유틸리티
# ──────────────────────────────────────────────

def log(msg: str) -> None:
    """타임스탬프 포함 로그 출력"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def html_escape(text: str) -> str:
    """텔레그램 HTML 모드 특수문자 이스케이프"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_num(n: int) -> str:
    """숫자를 K/M 단위로 표시"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def send_telegram(message: str) -> bool:
    """
    텔레그램 HTML 메시지 전송.
    4096자 초과 시 자동 분할.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # 줄 단위 분할
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
                log(f"텔레그램 전송 실패 (chunk {i+1}): {resp.status_code} {resp.text[:100]}")
                success = False
        except Exception as e:
            log(f"텔레그램 예외: {e}")
            success = False
        if i < len(chunks) - 1:
            time.sleep(0.5)
    return success


def extract_korean_tags(text: str) -> List[str]:
    """
    텍스트에서 한글 해시태그 및 한국어 키워드 추출.
    #해시태그 패턴 우선, 이후 2~7글자 한글 단어.
    """
    tags: List[str] = []
    # #해시태그 패턴
    tags += re.findall(r"#[가-힣]{2,10}", text)
    # 일반 한글 단어 (2~7글자)
    tags += re.findall(r"[가-힣]{2,7}", text)
    return tags


def filter_tags(tags: List[str]) -> List[str]:
    """불용어 제거 및 최소 길이 필터"""
    result = []
    for tag in tags:
        clean = tag.lstrip("#")
        if clean not in STOPWORDS and len(clean) >= 2:
            result.append(tag if tag.startswith("#") else f"#{clean}")
    return result


# ──────────────────────────────────────────────
# 소스 1: Naver signal.bz 실시간 급상승 검색어
# ──────────────────────────────────────────────

def fetch_naver_realtime() -> List[str]:
    """
    signal.bz 비공식 API로 네이버 실시간 급상승 검색어 수집.
    반환: 한글 키워드 리스트 (# 없음, 그대로 반환)
    """
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
                kw = (item.get("keyword") or item.get("title") or "") if isinstance(item, dict) else item
                if kw:
                    keywords.append(kw)

        # 한글만 필터
        korean_only = [kw for kw in keywords if re.search(r"[가-힣]", kw)]
        log(f"[Naver signal.bz] {len(korean_only)}개 수집")
        return korean_only

    except Exception as e:
        log(f"[Naver signal.bz 실패] {e}")
        return []


# ──────────────────────────────────────────────
# 소스 2: Naver 검색 연관 해시태그
# ──────────────────────────────────────────────

def fetch_naver_related_tags(query: str) -> List[str]:
    """
    네이버 검색 결과 HTML에서 인스타 연관 해시태그 추출.
    네이버 검색은 '#오늘코디' 같은 태그를 VIEW 섹션에 노출.
    """
    try:
        url = "https://search.naver.com/search.naver"
        params = {"query": query, "where": "nexearch"}
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if resp.status_code != 200:
            return []

        html = resp.text
        # 방법 1: #한글태그 패턴 직접 추출
        tags = re.findall(r"#([가-힣]{2,10})", html)
        # 방법 2: data-tag 또는 tag-text 속성에서 추출
        tags += re.findall(r'data-tag="([가-힣]{2,10})"', html)
        tags += re.findall(r'class="tag[^"]*">([가-힣]{2,10})', html)

        # 중복 제거 후 반환
        seen: set = set()
        result = []
        for t in tags:
            if t not in seen and t not in STOPWORDS:
                seen.add(t)
                result.append(f"#{t}")
        return result[:15]  # 쿼리당 최대 15개

    except Exception as e:
        log(f"[Naver 연관태그 실패] {query}: {e}")
        return []


def fetch_all_naver_tags() -> List[str]:
    """
    여러 시드 쿼리로 네이버 연관 해시태그 일괄 수집.
    """
    all_tags: List[str] = []
    for query in NAVER_SEED_QUERIES:
        tags = fetch_naver_related_tags(query)
        all_tags.extend(tags)
        time.sleep(0.5)  # 네이버 rate limit 방지
    log(f"[Naver 연관태그] 총 {len(all_tags)}개 수집")
    return all_tags


# ──────────────────────────────────────────────
# 소스 3: Google Trends RSS — 한국 실시간 트렌드
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
        ns   = {"ht": "https://trends.google.com/trending/rss"}
        items = root.findall(".//item")

        tags: List[str] = []
        display: List[str] = []

        for item in items:
            title   = item.find("title")
            traffic = item.find("ht:approx_traffic", ns)
            if title is None:
                continue
            keyword = title.text.strip()
            traffic_txt = traffic.text.strip() if traffic is not None else ""

            # 한글 포함 키워드만 해시태그 후보로
            if re.search(r"[가-힣]", keyword):
                clean = re.sub(r"\s+", "", keyword)  # 공백 제거 (해시태그용)
                if clean not in STOPWORDS and len(clean) >= 2:
                    tags.append(f"#{clean}")

            # 표시용: 검색량 포함
            suffix = f" ({traffic_txt})" if traffic_txt else ""
            display.append(f"{keyword}{suffix}")

        log(f"[Google Trends] 키워드 {len(display)}개 수집, 태그 {len(tags)}개")
        return tags, display[:10]

    except Exception as e:
        log(f"[Google Trends 실패] {e}")
        return [], []


# ──────────────────────────────────────────────
# 집계 & 랭킹
# ──────────────────────────────────────────────

def rank_tags(
    naver_realtime: List[str],
    naver_related: List[str],
    google_tags: List[str],
) -> List[Tuple[str, int]]:
    """
    수집된 한글 해시태그 빈도 집계 후 TOP N 반환.
    Naver 실시간 급상승은 가중치 3배 부여 (신뢰도 높음).
    """
    counter: Counter = Counter()

    # Naver 실시간 — 가중치 3
    for kw in naver_realtime:
        tag = f"#{kw}" if not kw.startswith("#") else kw
        clean = tag.lstrip("#")
        if clean not in STOPWORDS and re.search(r"[가-힣]", clean):
            counter[tag] += 3

    # Naver 연관태그 — 가중치 2
    for tag in naver_related:
        clean = tag.lstrip("#")
        if clean not in STOPWORDS:
            counter[tag] += 2

    # Google Trends — 가중치 2
    for tag in google_tags:
        clean = tag.lstrip("#")
        if clean not in STOPWORDS:
            counter[tag] += 2

    return counter.most_common(TOP_N)


# ──────────────────────────────────────────────
# 메시지 포맷
# ──────────────────────────────────────────────

def build_message(
    top_tags: List[Tuple[str, int]],
    naver_realtime: List[str],
    google_trends: List[str],
    sources_ok: List[str],
) -> str:
    """텔레그램 HTML 메시지 생성"""
    now = datetime.now()
    date_str   = now.strftime("%Y-%m-%d")
    weekday_ko = WEEKDAY_KO[now.weekday()]
    hour       = now.hour
    ampm       = "오전" if hour < 12 else "오후"
    h12        = hour if hour <= 12 else hour - 12
    time_str   = f"{ampm} {h12:02d}:{now.strftime('%M')}"

    lines = [
        "📱 <b>한국 인스타그램 트렌드</b>",
        f"{date_str} ({weekday_ko}) {time_str}",
        "",
        "🔥 <b>인기 한글 해시태그 TOP 20</b>",
    ]

    if top_tags:
        for i, (tag, score) in enumerate(top_tags, 1):
            lines.append(f"{i}. {html_escape(tag)}  <i>(점수 {score})</i>")
    else:
        lines.append("• 수집된 태그 없음")

    lines.append("")
    lines.append("📈 <b>네이버 실시간 급상승</b>")
    if naver_realtime:
        kr_only = [kw for kw in naver_realtime if re.search(r"[가-힣]", kw)]
        lines.append("• " + " / ".join(kr_only[:10]))
    else:
        lines.append("• 데이터 없음")

    lines.append("")
    lines.append("🔍 <b>구글 실시간 트렌드 (한국)</b>")
    if google_trends:
        for item in google_trends:
            lines.append(f"• {html_escape(item)}")
    else:
        lines.append("• 데이터 없음")

    lines.append("")
    lines.append("━━━━━━━━━━━━━")
    lines.append(f"수집 소스: {' · '.join(sources_ok) if sources_ok else '없음'}")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# 상태 저장
# ──────────────────────────────────────────────

def save_state(top_tags: List[Tuple[str, int]]) -> None:
    """수집 결과 JSONL 이력 저장"""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().isoformat(),
            "tags": [{"tag": tag, "score": score} for tag, score in top_tags],
        }
        with SCORE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"[상태 저장 실패] {e}")


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def main() -> None:
    log("=== 한국 인스타 트렌드 수집 시작 ===")
    sources_ok: List[str] = []

    # ── 소스 1: Naver 실시간 급상승 ──
    naver_realtime = fetch_naver_realtime()
    if naver_realtime:
        sources_ok.append("Naver 실시간")

    # ── 소스 2: Naver 연관 해시태그 ──
    naver_related = fetch_all_naver_tags()
    if naver_related:
        sources_ok.append("Naver 연관태그")

    # ── 소스 3: Google Trends RSS ──
    google_tags, google_trends = fetch_google_trends()
    if google_trends:
        sources_ok.append("Google Trends")

    # ── 모든 소스 실패 ──
    if not sources_ok:
        send_telegram(
            "⚠️ <b>인스타 트렌드 수집 실패</b>\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            "모든 데이터 소스 수집 실패. 네트워크를 확인해 주세요."
        )
        sys.exit(1)

    # ── 집계 & 랭킹 ──
    top_tags = rank_tags(naver_realtime, naver_related, google_tags)

    # ── 메시지 전송 ──
    msg = build_message(top_tags, naver_realtime, google_trends, sources_ok)
    ok = send_telegram(msg)
    log(f"텔레그램 전송: {'성공' if ok else '실패'}")

    if ok:
        save_state(top_tags)

    log(f"=== 완료 | 소스: {', '.join(sources_ok)} | 태그: {len(top_tags)}개 ===")


if __name__ == "__main__":
    main()
