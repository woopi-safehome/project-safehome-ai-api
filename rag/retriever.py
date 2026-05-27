import chromadb

# 등기부 텍스트에서 탐지할 위험 신호 키워드
_RISK_SIGNALS = [
    # 등기부 갑구 위험 신호
    "신탁", "가등기", "압류", "가압류", "경매",
    "가처분", "소유권이전", "예고등기", "처분금지",
    "임의경매", "강제경매", "환매",
    # 등기부 을구 위험 신호
    "근저당", "전세권", "임차권등기", "채권최고액",
    "지상권", "구분지상권",
    # 사기 패턴 연관 신호
    "법인", "공유", "지분",
]

_DEFAULT_QUERY_전세 = "임차인 보증금 보호 전세사기 위험 대항력 우선변제권"
_DEFAULT_QUERY_월세 = "월세 임차인 소액보증금 최우선변제권 경매 퇴거 대항력"

# 체크리스트 항목별 위험 심각도 우선순위
# 앞에 있을수록 더 즉각적이고 치명적인 위험
_RISK_PRIORITY = [
    "auction",             # 경매 진행 중 — 계약 즉시 불가
    "trust",               # 신탁등기 — 계약 자체 무효 가능
    "preliminary",         # 가등기 — 본등기 시 임차권 소멸
    "seizure",             # 압류 — 국가 우선 변제
    "provisional_seizure", # 가압류·가처분 — 채무 분쟁 진행 중
    "mortgage_scale",      # 근저당 과다 — 경매 시 보증금 회수 불확실
    "transfer_frequency",  # 잦은 소유권 이전 — 갭투자 의심
    "lease_rights",        # 선순위 전세권·임차권 — 후순위로 밀림
    "senior_rights",       # 선순위 권리 존재
    "ownership_clarity",   # 공유지분 — 타 지분 경매 위험
    "surface_rights",      # 지상권 — 생활 제한 가능
]


def retrieve(collection: chromadb.Collection, sections: dict, n_results: int = 4, lease_type: str = None) -> str:
    """
    등기부 섹션에서 위험 신호를 감지하여 관련 법령·패턴 청크를 검색하고
    LLM에 주입할 컨텍스트 문자열로 반환.
    """
    query = _build_query(sections, lease_type)
    results = collection.query(query_texts=[query], n_results=n_results)

    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]

    if not docs:
        return ""

    parts = [
        f"[참고: {m['source']} {m['article']}]\n{doc}"
        for doc, m in zip(docs, metas)
    ]
    return "\n\n---\n\n".join(parts)


def retrieve_references(
    collection: chromadb.Collection,
    checklist: list,
    safety_level: str,
) -> dict:
    """
    위험/주의 체크리스트 항목들에 대해 법령·사례를 항목당 1건씩 검색하고
    중복을 제거하여 반환합니다.

    - SAFE → 빈 dict 반환
    - DANGER → "위험" 항목 대상, 없으면 "주의" 항목으로 폴백
    - CAUTION → "주의" 항목 대상
    """
    if safety_level == "SAFE":
        return {}

    target_status = "위험" if safety_level == "DANGER" else "주의"
    target_items = _filter_and_sort(checklist, target_status)

    # DANGER인데 "위험" 항목이 하나도 없는 경우는 없지만, 방어적으로 폴백
    if not target_items and safety_level == "DANGER":
        target_items = _filter_and_sort(checklist, "주의")

    seen_law_articles: set[str] = set()
    seen_case_articles: set[str] = set()
    laws: list[dict] = []
    cases: list[dict] = []

    for item in target_items:
        law, case = _retrieve_one(collection, item)
        if law:
            key = law["article"]
            if key not in seen_law_articles:
                seen_law_articles.add(key)
                laws.append(law)
        if case:
            key = case["article"]
            if key not in seen_case_articles:
                seen_case_articles.add(key)
                cases.append(case)

    references = {}
    if laws:
        references["laws"] = laws
    if cases:
        references["cases"] = cases
    return references


def _filter_and_sort(checklist: list, status: str) -> list:
    """해당 status 항목을 _RISK_PRIORITY 순으로 정렬하여 반환."""
    items = [item for item in checklist if item.get("status") == status]
    return sorted(
        items,
        key=lambda x: _RISK_PRIORITY.index(x["id"]) if x["id"] in _RISK_PRIORITY else len(_RISK_PRIORITY),
    )


def _retrieve_one(
    collection: chromadb.Collection,
    checklist_item: dict,
) -> tuple[dict | None, dict | None]:
    """체크리스트 항목 하나에 대해 유사도 상위 법령 1건 + 사례 1건을 반환합니다."""
    analysis = checklist_item.get("analysis") or {}
    query = analysis.get("findings") or checklist_item.get("item", "")

    results = collection.query(
        query_texts=[query],
        n_results=10,
        include=["documents", "metadatas"],
    )
    metas = results.get("metadatas", [[]])[0]

    law = None
    case = None

    for meta in metas:
        is_case = meta.get("source") == "실제 사례 분석"
        if law is None and not is_case:
            law = _format_chunk(meta)
        if case is None and is_case:
            case = _format_chunk(meta)
        if law and case:
            break

    return law, case


def _format_chunk(meta: dict) -> dict:
    chunk = {
        "source": meta.get("source", ""),
        "article": meta.get("article", ""),
        "title": meta.get("title", ""),
        "content": meta.get("content", ""),
        "riskContext": meta.get("risk_context", ""),
    }
    tags = meta.get("tags", "")
    chunk["tags"] = tags.split(",") if tags else []
    return chunk


def _build_query(sections: dict, lease_type: str = None) -> str:
    """등기부 섹션 텍스트에서 위험 키워드를 추출하여 검색 쿼리 생성."""
    all_text = " ".join(
        " ".join(v) if isinstance(v, list) else str(v)
        for v in sections.values()
    )
    found = [kw for kw in _RISK_SIGNALS if kw in all_text]
    if found:
        base = " ".join(found)
        return base + " 소액보증금 최우선변제권" if lease_type == "월세" else base
    return _DEFAULT_QUERY_월세 if lease_type == "월세" else _DEFAULT_QUERY_전세
