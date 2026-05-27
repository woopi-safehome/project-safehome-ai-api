"""
Daily RAG updater: collect news & law amendments → ChromaDB 자동 반영.

Usage:
    python rag/updater.py              # 최근 1일치 뉴스 + 7일치 법령
    python rag/updater.py --days 3     # 최근 3일치 뉴스

로그: logs/update_YYYYMMDD.log
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import chromadb
import feedparser
import requests
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_ROOT = Path(__file__).parent.parent
_LOG_DIR = _ROOT / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_TODAY = datetime.now().strftime("%Y%m%d")
_LOG_FILE = _LOG_DIR / f"update_{_TODAY}.log"

_CHROMA_PATH = str(_ROOT / "chroma_db")
_COLLECTION_NAME = "legal_knowledge"

_file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(message)s"))

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter("%(message)s"))

# basicConfig는 루트 로거에 핸들러가 없을 때만 동작하므로,
# app.py가 먼저 basicConfig를 호출한 뒤 scheduler에서 import되면 no-op이 됨.
# 대신 이 모듈 전용 logger에 직접 핸들러를 붙여 어느 컨텍스트에서도 파일·콘솔 출력을 보장.
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(_file_handler)
    logger.addHandler(_console_handler)
logger.propagate = False


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_success(item_type: str, chunk: dict, cid: str) -> None:
    logger.info(
        "[%s] 성공  %-4s  %s\n"
        "           제목: %s\n"
        "           출처: %s  %s\n"
        "           태그: %s\n"
        "           ID  : %s",
        _ts(), item_type.upper(),
        "",
        chunk.get("title", ""),
        chunk.get("source", ""), chunk.get("article", ""),
        ", ".join(chunk.get("tags", [])),
        cid,
    )


def _log_skip_duplicate(item_type: str, chunk: dict) -> None:
    logger.info(
        "[%s] 건너뜀(중복)  %-4s  %s",
        _ts(), item_type.upper(), chunk.get("title", ""),
    )


def _log_skip_irrelevant(item_type: str, label: str) -> None:
    logger.info(
        "[%s] 건너뜀(무관)  %-4s  %s",
        _ts(), item_type.upper(), label,
    )


def _log_fail(item_type: str, label: str, reason: str) -> None:
    logger.info(
        "[%s] 실패  %-4s  %s\n"
        "           사유: %s",
        _ts(), item_type.upper(), label, reason,
    )

_NEWS_QUERIES = [
    "전세사기",
    "주택임대차보호법 개정",
    "전세보증금 피해",
    "등기부등본 위험",
    "임대차 분쟁",
]

_LAW_NAMES = [
    "주택임대차보호법",
    "전세사기피해자 지원 및 주거안정에 관한 특별법",
]

_CHUNK_SCHEMA = """{
  "source": "출처 (뉴스사명 또는 법령명)",
  "article": "조항 또는 보도일자 (예: 2025-05-16)",
  "title": "핵심 주제 제목 30자 이내",
  "content": "핵심 내용 요약 300자 이내",
  "keywords": ["키워드1", "키워드2"],
  "risk_context": "임차인 관점 위험 맥락 또는 주의 사항 150자 이내",
  "tags": ["태그1", "태그2"]
}"""


# ---------------------------------------------------------------------------
# ChromaDB
# ---------------------------------------------------------------------------

def get_collection(api_key: str) -> chromadb.Collection:
    client = chromadb.PersistentClient(path=_CHROMA_PATH)
    ef = OpenAIEmbeddingFunction(api_key=api_key, model_name="text-embedding-3-small")
    return client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )


# ---------------------------------------------------------------------------
# News collection (Google News RSS — no API key required)
# ---------------------------------------------------------------------------

def fetch_news(queries: list[str], days_back: int = 1) -> list[dict]:
    articles = []
    seen_titles: set[str] = set()
    since = datetime.now() - timedelta(days=days_back)

    for query in queries:
        url = (
            "https://news.google.com/rss/search"
            f"?q={requests.utils.quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
        )
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                if not title or title in seen_titles:
                    continue
                pub = entry.get("published_parsed")
                if pub and datetime(*pub[:6]) < since:
                    continue
                seen_titles.add(title)
                articles.append({
                    "title": title,
                    "link": entry.get("link", ""),
                    "summary": entry.get("summary", ""),
                    "published": entry.get("published", ""),
                    "query": query,
                })
        except Exception as exc:
            logger.info("[%s] 실패  NEWS  RSS 파싱 오류 [%s]\n           사유: %s", _ts(), query, exc)

    logger.info("[%s] 뉴스 수집: %d건", _ts(), len(articles))
    return articles


# ---------------------------------------------------------------------------
# Law amendment collection (국가법령정보센터 Open API)
# 발급: https://open.law.go.kr → 오픈API → 이용신청 (무료)
# ---------------------------------------------------------------------------

def fetch_law_amendments(api_key: str, days_back: int = 7) -> list[dict]:
    if not api_key:
        logger.info("[%s] 건너뜀(무관)  LAW   LAW_API_KEY 미설정", _ts())
        return []

    since_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
    amendments = []

    for law_name in _LAW_NAMES:
        url = (
            "https://www.law.go.kr/DRF/lawSearch.do"
            f"?OC={api_key}&target=law&type=JSON"
            f"&query={requests.utils.quote(law_name)}&display=5&page=1"
        )
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            laws = resp.json().get("LawSearch", {}).get("law", [])
            for law in laws:
                prom_date = str(law.get("공포일자", ""))
                if prom_date >= since_date:
                    amendments.append({
                        "name": law.get("법령명한글", law_name),
                        "promulgation_date": prom_date,
                        "law_id": str(law.get("법령ID", "")),
                    })
        except Exception as exc:
            logger.info("[%s] 실패  LAW   API 조회 오류 [%s]\n           사유: %s", _ts(), law_name, exc)

    logger.info("[%s] 법령 개정 수집: %d건", _ts(), len(amendments))
    return amendments


def fetch_law_detail(api_key: str, law_id: str) -> str:
    try:
        url = (
            "https://www.law.go.kr/DRF/lawService.do"
            f"?OC={api_key}&target=law&ID={law_id}&type=JSON"
        )
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        units = resp.json().get("법령", {}).get("조문", {}).get("조문단위", [])
        parts = []
        for unit in units[:10]:
            t = unit.get("조문제목", "")
            c = unit.get("조문내용", "")
            if t or c:
                parts.append(f"{t}: {c}".strip())
        return "\n".join(parts)
    except Exception as exc:
        logger.info("[%s] 실패  LAW   상세 조회 오류 [ID=%s]\n           사유: %s", _ts(), law_id, exc)
        return ""


# ---------------------------------------------------------------------------
# GPT conversion
# ---------------------------------------------------------------------------

def convert_to_chunk(client: OpenAI, item_type: str, raw: dict) -> dict | None:
    if item_type == "news":
        user_content = (
            f"제목: {raw['title']}\n"
            f"요약: {raw['summary']}\n"
            f"날짜: {raw['published']}\n"
            f"검색 키워드: {raw['query']}"
        )
    else:
        user_content = (
            f"법령명: {raw['name']}\n"
            f"공포일자: {raw['promulgation_date']}\n"
            f"조문 내용:\n{raw.get('detail', '(없음)')}"
        )

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "당신은 전세사기·부동산 임대차 전문 데이터 큐레이터입니다.\n"
                        "주어진 뉴스나 법령을 임차인 보호 관점에서 RAG 청크 JSON으로 변환하세요.\n"
                        "부동산 임대차와 무관한 내용이면 반드시 {\"skip\": true} 만 반환하세요.\n\n"
                        f"출력 스키마 (JSON):\n{_CHUNK_SCHEMA}"
                    ),
                },
                {"role": "user", "content": user_content},
            ],
        )
        result = json.loads(resp.choices[0].message.content)
        return None if result.get("skip") else result
    except Exception as exc:
        logger.warning("청크 변환 실패: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_id(chunk: dict, item_type: str, raw: dict) -> str:
    date_hint = raw.get("published", raw.get("promulgation_date", ""))
    seed = f"{item_type}_{chunk.get('source', '')}_{chunk.get('title', '')}_{date_hint}"
    return f"auto_{hashlib.md5(seed.encode()).hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(days_back: int = 1) -> None:
    openai_key = os.getenv("OPENAI_API_KEY")
    law_api_key = os.getenv("LAW_API_KEY", "")

    if not openai_key:
        logger.error("OPENAI_API_KEY가 설정되지 않았습니다.")
        sys.exit(1)

    logger.info("=" * 64)
    logger.info("[%s] RAG 업데이트 시작  (최근 %d일치)", _ts(), days_back)
    logger.info("=" * 64)

    openai_client = OpenAI(api_key=openai_key)

    try:
        collection = get_collection(openai_key)
        existing_ids = set(collection.get(include=[])["ids"])
        logger.info("[%s] 컬렉션 연결 성공  현재 %d건", _ts(), len(existing_ids))
    except Exception as exc:
        logger.info("[%s] 실패  ChromaDB 연결 불가\n           사유: %s", _ts(), exc)
        sys.exit(1)

    added: list[dict] = []
    skipped_dup = 0
    skipped_irr = 0
    failed: list[dict] = []

    def process(item_type: str, raw: dict, label: str) -> None:
        nonlocal skipped_dup, skipped_irr

        chunk = convert_to_chunk(openai_client, item_type, raw)
        if chunk is None:
            _log_skip_irrelevant(item_type, label)
            skipped_irr += 1
            return

        cid = make_id(chunk, item_type, raw)
        if cid in existing_ids:
            _log_skip_duplicate(item_type, chunk)
            skipped_dup += 1
            return

        try:
            collection.add(
                ids=[cid],
                documents=[
                    f"{chunk['title']}\n{chunk['content']}\n위험 맥락: {chunk['risk_context']}"
                ],
                metadatas=[{
                    "source": chunk.get("source", ""),
                    "article": chunk.get("article", ""),
                    "title": chunk.get("title", ""),
                    "tags": ",".join(chunk.get("tags", [])),
                }],
            )
            existing_ids.add(cid)
            added.append(chunk)
            _log_success(item_type, chunk, cid)
        except Exception as exc:
            reason = f"ChromaDB 저장 실패: {exc}"
            _log_fail(item_type, label, reason)
            failed.append({"label": label, "reason": reason})

    # 1. 뉴스
    logger.info("")
    logger.info("[%s] ── 뉴스 수집 ──────────────────────────────────", _ts())
    news_items = fetch_news(_NEWS_QUERIES, days_back=days_back)
    if not news_items:
        logger.info("[%s] 실패  NEWS  수집된 기사 없음 (네트워크 또는 RSS 오류)", _ts())
        failed.append({"label": "뉴스 RSS", "reason": "수집된 기사 없음"})
    for raw in news_items:
        process("news", raw, raw["title"])

    # 2. 법령 개정
    logger.info("")
    logger.info("[%s] ── 법령 개정 수집 ─────────────────────────────", _ts())
    law_items = fetch_law_amendments(law_api_key, days_back=max(days_back, 7))
    for raw in law_items:
        if law_api_key and raw.get("law_id"):
            raw["detail"] = fetch_law_detail(law_api_key, raw["law_id"])
        process("law", raw, raw["name"])

    # 결과 요약
    logger.info("")
    logger.info("=" * 64)
    logger.info("[%s] 업데이트 완료", _ts())
    logger.info("           성공(추가)       : %d건", len(added))
    logger.info("           건너뜀(중복)     : %d건", skipped_dup)
    logger.info("           건너뜀(무관)     : %d건", skipped_irr)
    logger.info("           실패             : %d건", len(failed))
    if failed:
        for f in failed:
            logger.info("             · %s — %s", f["label"], f["reason"])
    logger.info("           컬렉션 총        : %d건", collection.count())
    logger.info("=" * 64)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1, help="수집 기간(일), 기본값 1")
    args = parser.parse_args()
    run(days_back=args.days)
