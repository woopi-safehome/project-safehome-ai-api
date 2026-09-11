"""
판정 — 모델이 추출한 사실로 체크리스트 상태와 안전 등급을 계산하고 응답을 조립한다.

이 서비스의 핵심 결정이 여기에 있다. 모델은 사실 추출과 서술만 하고,
등급 판정은 이 모듈이 결정적으로 계산한다. 같은 입력에 같은 등급이 나와야
상류의 캐시 전략이 성립하기 때문이다.

배경: README.md 의 "설계 원칙 — 역할 분리"
"""

def _compute_checklist(llm_output: dict, checklist_analysis: dict, lease_type: str = None) -> list:
    """
    LLM이 추출한 사실 데이터로부터 11개 체크리스트 항목의 status를 결정적으로 계산하고,
    LLM이 생성한 checklistAnalysis(findings + leaseImpact)를 각 항목에 병합합니다.
    """
    ownership  = llm_output.get("ownershipInfo") or {}
    mortgage   = llm_output.get("mortgageInfo") or {}
    other      = llm_output.get("otherRights") or []
    risks      = llm_output.get("legalRisks") or []

    def has_risk(*keywords):
        return any(
            any(kw in (r.get("type") or "") for kw in keywords)
            for r in risks
        )

    active_count   = mortgage.get("activeCount") or 0
    total_amount   = mortgage.get("totalMaxClaimAmount") or "없음"
    is_shared      = "공유" in (ownership.get("ownerType") or "")
    freq_warning   = bool(ownership.get("frequentTransferWarning"))
    transfer_count = ownership.get("transferCount") or 0
    recent_date    = ownership.get("recentTransferDate") or ""

    has_provisional = has_risk("가압류", "가처분", "처분금지")
    has_seizure     = any(
        "압류" in (r.get("type") or "") and "가압류" not in (r.get("type") or "")
        for r in risks
    )
    has_auction     = has_risk("경매")
    has_trust       = any("신탁" in (r.get("type") or "") for r in other)
    has_preliminary = any("가등기" in (r.get("type") or "") for r in other)
    has_lease_right = any(
        any(kw in (r.get("type") or "") for kw in ["전세권", "임차권"])
        for r in other
    )
    has_surface     = any(
        any(kw in (r.get("type") or "") for kw in ["지상권", "구분지상권"])
        for r in other
    )
    has_senior      = active_count > 0 or has_lease_right
    is_wolse        = lease_type == "월세"

    def _item(id_, category, item, status, detail):
        entry = {
            "id": id_,
            "category": category,
            "item": item,
            "status": status,
            "detail": detail,
        }
        ca = checklist_analysis.get(id_)
        if ca:
            entry["analysis"] = {
                "findings": ca.get("findings", ""),
                "leaseImpact": ca.get("leaseImpact", ""),
            }
        return entry

    return [
        # ── 소유권 ──────────────────────────────────────────
        _item(
            "ownership_clarity", "소유권",
            "소유권 명확성 (단독소유 여부, 공유지분 위험)",
            "주의" if is_shared else "양호",
            (
                "공유지분 소유로 확인되었습니다. 다른 공유자의 채무로 인해 해당 지분이 경매될 수 있어 임차인 보증금이 위험할 수 있습니다."
                if is_shared else
                "단독소유로 확인되어 공유지분으로 인한 강제경매 위험이 없습니다."
            ),
        ),
        _item(
            "transfer_frequency", "소유권",
            f"소유권 이전 빈도 (단기간 잦은 이전 — {'임대차 사기' if is_wolse else '전세사기'} 주요 패턴)",
            (
                "위험" if freq_warning
                else "주의" if transfer_count >= 1 and recent_date
                else "양호"
            ),
            (
                f"최근 3년 이내 소유권 이전이 {transfer_count}회 확인되었습니다. 갭투자 또는 {'임대차 사기' if is_wolse else '전세사기'} 의심 패턴입니다."
                if freq_warning else
                f"최근 소유권 이전({recent_date}) 이력이 있습니다. 취득 경위를 추가 확인하세요."
                if transfer_count >= 1 and recent_date else
                "최근 3년간 잦은 소유권 이전이 확인되지 않아 안정적입니다."
            ),
        ),

        # ── 담보권 ──────────────────────────────────────────
        _item(
            "mortgage_scale", "담보권",
            "근저당 설정 규모 (임차보증금 + 선순위 담보 합산 위험)",
            (
                "양호" if active_count == 0
                else "위험" if active_count >= 3
                else "주의"
            ),
            (
                ("활성 근저당권이 설정되어 있지 않아 경매 발생 시 퇴거 위험이 낮습니다." if is_wolse else "활성 근저당권이 설정되어 있지 않아 보증금 전액 회수 가능성이 높습니다.")
                if active_count == 0 else
                f"활성 근저당권 {active_count}건(채권최고액 합산 {total_amount}) 확인. {'경매 시 퇴거 및 소액 보증금 손실 위험이 있습니다.' if is_wolse else '경매 시 보증금 회수가 불확실합니다.'}"
                if active_count >= 3 else
                f"활성 근저당권 {active_count}건(채권최고액 합산 {total_amount}) 확인. 임차 전 담보 비율을 확인하세요."
            ),
        ),
        _item(
            "senior_rights", "담보권",
            "선순위 권리 존재 여부 (선순위 담보·전세권이 임차인보다 우선 변제)",
            "주의" if has_senior else "양호",
            (
                ("선순위 근저당 또는 전세권이 확인됩니다. 경매 시 해당 권리가 임차인보다 먼저 변제되며, 소액 최우선변제권 적용 여부를 확인하세요." if is_wolse else "선순위 근저당 또는 전세권이 확인됩니다. 경매 시 해당 권리가 임차인보다 먼저 변제됩니다.")
                if has_senior else
                "선순위 담보·전세권이 없어 임차인 보증금이 우선 보호될 수 있는 조건입니다."
            ),
        ),

        # ── 법적위험 ─────────────────────────────────────────
        _item(
            "provisional_seizure", "법적위험",
            "가압류·가처분 여부 (임대인의 채무 분쟁 신호)",
            "위험" if has_provisional else "양호",
            (
                "가압류·가처분 등기가 확인됩니다. 임대인 채무 미이행 시 경매로 이어질 수 있습니다."
                if has_provisional else
                "가압류·가처분 등기가 없어 임대인의 채무 분쟁이 확인되지 않습니다."
            ),
        ),
        _item(
            "seizure", "법적위험",
            "압류 여부 (세금 체납 — 국세·지방세 체납 시 국가가 선순위)",
            "위험" if has_seizure else "양호",
            (
                "압류 등기가 확인됩니다. 국세 또는 지방세 체납의 경우 임차인 보증금보다 국가가 우선 변제됩니다."
                if has_seizure else
                "압류 등기가 없어 세금 체납이 확인되지 않습니다."
            ),
        ),
        _item(
            "auction", "법적위험",
            "경매 진행 여부 (경매개시결정 등기 시 계약 즉시 위험)",
            "위험" if has_auction else "양호",
            (
                "경매개시결정 등기가 확인됩니다. 계약 체결 시 보증금 전액 손실 가능성이 있습니다."
                if has_auction else
                "경매개시결정 등기가 없어 경매가 진행 중이지 않습니다."
            ),
        ),

        # ── 특수권리 ─────────────────────────────────────────
        _item(
            "lease_rights", "특수권리",
            "선순위 전세권·임차권 등기 현황 (기존 임차인 존재 여부)",
            "주의" if has_lease_right else "양호",
            (
                "선순위 전세권·임차권 등기가 확인됩니다. 경매 시 해당 금액이 먼저 변제됩니다."
                if has_lease_right else
                "선순위 전세권·임차권 등기가 없어 기존 임차인으로 인한 보증금 위험이 없습니다."
            ),
        ),
        _item(
            "trust", "특수권리",
            "신탁등기 여부 (신탁된 부동산은 수탁자 동의 없는 임대차 계약이 무효 가능)",
            "위험" if has_trust else "양호",
            (
                "신탁등기가 확인됩니다. 수탁자(신탁회사) 동의 없이 체결한 임대차는 대항력이 없을 수 있습니다."
                if has_trust else
                "신탁등기가 없어 관련 위험이 없습니다."
            ),
        ),
        _item(
            "preliminary", "특수권리",
            "가등기 여부 (소유권이전청구권 가등기는 본등기 시 임차권 소멸 가능)",
            "위험" if has_preliminary else "양호",
            (
                "가등기가 확인됩니다. 본등기 완료 시 임차권이 소멸할 수 있습니다."
                if has_preliminary else
                "가등기가 없어 관련 위험이 없습니다."
            ),
        ),
        _item(
            "surface_rights", "특수권리",
            "지상권·구분지상권 설정 여부 (건물 사용 제한 가능성)",
            "주의" if has_surface else "양호",
            (
                "지상권·구분지상권 설정이 확인됩니다. 지상권자가 토지 사용 권한을 가지므로 임차 생활에 제한이 있을 수 있습니다."
                if has_surface else
                "지상권·구분지상권이 설정되어 있지 않습니다."
            ),
        ),
    ]

def _compute_safety_level(checklist: list) -> str:
    """
    checklist 항목 status 기반으로 안전 등급을 결정적으로 계산합니다.
      - '위험' 하나라도 있으면 → DANGER
      - '위험' 없고 '주의' 있으면 → CAUTION
      - 모두 '양호' → SAFE
    """
    statuses = {item.get("status") for item in checklist}
    if "위험" in statuses:
        return "DANGER"
    if "주의" in statuses:
        return "CAUTION"
    return "SAFE"

def _build_response(llm_output: dict, checklist: list, safety_level: str, references: dict) -> dict:
    """
    LLM 출력과 Python 계산 결과를 클라이언트 응답 구조로 조합합니다.
    내부 추출 데이터(mortgageInfo, otherRights, legalRisks)는 제외합니다.
    """
    response = {
        "isValidDeed": True,
        "safetyLevel": safety_level,
        "analysisSummary": llm_output.get("analysisSummary"),
        "propertyInfo": llm_output.get("propertyInfo"),
        "ownershipInfo": llm_output.get("ownershipInfo"),
        "checklist": checklist,
        "riskSummary": llm_output.get("riskSummary"),
        "overallSummary": llm_output.get("overallSummary"),
        "recommendations": llm_output.get("recommendations"),
    }
    if references:
        response["references"] = references
    return response
