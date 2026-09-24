"""
판정 — 모델이 추출한 사실로 체크리스트 상태와 안전 등급을 계산하고 응답을 조립한다.

이 서비스의 핵심 결정이 여기에 있다. 모델은 사실 추출과 서술만 하고,
등급 판정은 이 모듈이 결정적으로 계산한다. 같은 입력에 같은 등급이 나와야
상류의 캐시 전략이 성립하기 때문이다.

배경: README.md 의 "설계 원칙 — 역할 분리"
"""

import re
from datetime import date


# ─────────────────────────────────────────────────────────────
# 등기 항목 → 사실
#
# 모델은 갑구·을구 항목을 순위번호·등기목적·날짜·금액 그대로 **베껴 적기만** 한다.
# 어느 권리가 말소됐는지, 살아 있는 것이 몇 건이고 합계가 얼마인지는 여기서 센다.
#
# 모델에게 맡기지 않는 이유: PDF 에서 글자를 뽑으면 말소 표시(취소선)가 사라진다. 말소는
# 뒤 순위의 "N번○○등기말소" 로만 드러나는데, 모델은 이것을 앞 항목과 짝지어 지우는 일을
# 자주 틀렸다 — 실제 등기부에서 모두 말소된 압류 3건을 '위험'으로 잡고, 활성 근저당 3건·약 3.1억을
# 6건·1.5억으로 세었다. 짝짓기와 합산은 규칙이므로 코드가 한다.
# ─────────────────────────────────────────────────────────────

# 앞 순위를 가리키는 부기 항목("3번압류등기말소", "1번근저당권변경"). 새 권리가 아니다.
_REFERS_TO_RANK = re.compile(r"^\s*\d+(?:-\d+)?\s*번")
_RANK_REF = re.compile(r"(\d+(?:-\d+)?)\s*번")

_UNITS = (("억", 100_000_000), ("천만", 10_000_000), ("백만", 1_000_000), ("만", 10_000), ("천", 1_000))


def _won(text) -> int | None:
    """금액 문자열을 원 단위 정수로. "금138,960,000원" · "금1억2천만원" 모두 받는다. 읽을 수 없으면 None."""
    if text is None:
        return None
    s = re.sub(r"[\s,]", "", str(text))
    if re.search(r"\d+(억|천만|백만|만|천)", s):
        total = 0
        for num, unit in re.findall(r"(\d+)(억|천만|백만|만|천)", s):
            total += int(num) * dict(_UNITS)[unit]
        return total or None
    digits = re.sub(r"\D", "", s)
    return int(digits) if digits else None


def _format_won(amount: int) -> str:
    return f"금{amount:,}원"


def _date(text) -> str | None:
    """"2013년10월25일" · "2013-10-25" · "2013.10.25" 를 YYYY-MM-DD 로. 없으면 None."""
    if not text:
        return None
    m = re.search(r"(\d{4})\s*[년.\-/]\s*(\d{1,2})\s*[월.\-/]\s*(\d{1,2})", str(text))
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def _legal_type(purpose: str) -> str | None:
    """갑구의 등기목적 → 법적 위험 종류. 판정 코드가 부분 일치로 읽는 값과 맞춘다."""
    if "경매개시결정" in purpose:
        return "강제경매개시결정" if "강제" in purpose else "임의경매개시결정"
    if "가압류" in purpose:
        return "가압류"
    if "가처분" in purpose:
        return "처분금지가처분" if "처분금지" in purpose else "가처분"
    if "압류" in purpose:
        return "압류"
    if "예고등기" in purpose:
        return "예고등기"
    return None


def _other_type(purpose: str) -> str | None:
    """등기목적 → 그 밖의 권리 종류. 소유권 이전의 원인으로 쓰인 "신탁재산귀속" 같은 것은 여기 오지 않는다."""
    if "가등기" in purpose:
        return "가등기"
    if "신탁" in purpose and "소유권" not in purpose:
        return "신탁"
    if "전세권" in purpose:
        return "전세권"
    if "임차권" in purpose:
        return "임차권"
    if "구분지상권" in purpose:
        return "구분지상권"
    if "지상권" in purpose:
        return "지상권"
    if "지역권" in purpose:
        return "지역권"
    if "환매" in purpose:
        return "환매특약"
    return None


def _cancelled_ranks(entries: list) -> dict:
    """
    구역별로 말소된 순위번호를 모은다. {"갑구": {"3", "5"}, "을구": {...}}

    **등기목적과 원인을 합쳐서 본다.** 등기목적 칸이 두 줄로 나뉘면 PDF 에서 뽑은 글자의 순서가 섞여,
    "3번근저당권설정등기말소" 가 목적 "3번근저당권설정등" + 원인 "…기말소" 로 갈라져 온다.
    목적만 보면 말소를 놓쳐 이미 지워진 근저당을 살아 있는 것으로 센다 — 실제 등기부에서 그랬다.
    가리키는 순위번호는 목적에서만 읽는다. 원인의 날짜 숫자를 순위로 오인하지 않게.
    """
    cancelled: dict = {}
    for e in entries:
        purpose = (e.get("purpose") or "").replace(" ", "")
        joined = purpose + (e.get("cause") or "").replace(" ", "")
        if _REFERS_TO_RANK.match(purpose) and "말소" in joined and "회복" not in joined:
            cancelled.setdefault(e.get("section"), set()).update(_RANK_REF.findall(purpose))
    return cancelled


def _derive_facts(entries: list, today: date) -> dict:
    """
    모델이 베껴 적은 등기 항목에서 판정에 쓰는 사실을 계산한다.

    돌려주는 모양은 _compute_checklist 가 읽는 것과 같다 — mortgageInfo · legalRisks · otherRights ·
    ownershipInfo(이전 이력 부분). 말소된 권리는 빼고, 서술에 쓰도록 따로 모아 둔다(cancelledRights).
    """
    entries = [e for e in (entries or []) if isinstance(e, dict)]
    cancelled = _cancelled_ranks(entries)

    def is_cancelled(e) -> bool:
        return str(e.get("rank", "")).strip() in cancelled.get(e.get("section"), set())

    mortgages, legal, other, gone, acquisitions = [], [], [], [], []
    for e in entries:
        purpose = (e.get("purpose") or "").replace(" ", "")
        section = e.get("section")
        if not purpose or _REFERS_TO_RANK.match(purpose):
            continue  # 말소·변경·이전 같은 부기 항목 — 새 권리가 아니다

        if section == "갑구" and ("소유권이전" in purpose or "소유권보존" in purpose) and "가등기" not in purpose:
            acquisitions.append(e)
            continue

        if "근저당" in purpose and "설정" in purpose:
            item = {
                "rank": e.get("rank"),
                "creditor": e.get("holder"),
                "maxClaimAmount": e.get("amount"),
                "registrationDate": _date(e.get("date")),
            }
            (gone if is_cancelled(e) else mortgages).append({**item, "type": "근저당권"})
            continue

        kind = _legal_type(purpose) if section == "갑구" else None
        if kind:
            item = {"type": kind, "rank": e.get("rank"), "claimant": e.get("holder"),
                    "amount": e.get("amount"), "registrationDate": _date(e.get("date"))}
            (gone if is_cancelled(e) else legal).append(item)
            continue

        kind = _other_type(purpose)
        if kind:
            item = {"type": kind, "rank": e.get("rank"), "holder": e.get("holder"),
                    "amount": e.get("amount"), "registrationDate": _date(e.get("date"))}
            (gone if is_cancelled(e) else other).append(item)

    amounts = [_won(m["maxClaimAmount"]) for m in mortgages]
    known = [a for a in amounts if a is not None]
    total = sum(known)

    # 소유권 이전 — 원인 날짜가 있으면 그것을, 없으면 접수일을 쓴다
    history = []
    for i, e in enumerate(acquisitions):
        cause = e.get("cause") or ""
        history.append({
            "owner": e.get("holder"),
            "acquisitionDate": _date(cause) or _date(e.get("date")) or "확인불가",
            "acquisitionCause": re.sub(r"\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일", "", cause).strip() or None,
            "isCurrent": i == len(acquisitions) - 1,
        })
    transfers = [e for e in acquisitions if "소유권이전" in (e.get("purpose") or "").replace(" ", "")]
    # 2월 29일에서 3년을 빼면 없는 날짜가 된다
    three_years_ago = today.replace(year=today.year - 3, day=min(today.day, 28) if today.month == 2 else today.day)
    recent_transfers = [
        e for e in transfers
        if (d := _date(e.get("cause")) or _date(e.get("date"))) and date.fromisoformat(d) >= three_years_ago
    ]
    current = history[-1] if history else {}

    return {
        "mortgageInfo": {
            "activeCount": len(mortgages),
            "totalMaxClaimAmount": _format_won(total) if mortgages else "없음",
            # 금액을 읽지 못한 근저당이 있으면 합계가 실제보다 적다. 서술이 그 사실을 말할 수 있게 남긴다.
            "unreadableAmountCount": len(amounts) - len(known),
            "details": mortgages,
        },
        "legalRisks": legal,
        "otherRights": other,
        "cancelledRights": gone,
        "ownershipInfo": {
            "transferCount": len(transfers),
            "recentTransferDate": current.get("acquisitionDate") if current else None,
            "recentTransferCause": current.get("acquisitionCause") if current else None,
            "frequentTransferWarning": len(recent_transfers) >= 2,
            "transferHistory": history,
        },
    }


def _merge_facts(extracted: dict, derived: dict) -> dict:
    """
    모델이 추출한 사실 위에 코드가 계산한 사실을 덮는다.

    소유자·소유 형태처럼 **읽기만 하면 되는 것**은 모델 것을 쓰고, 건수·합계·말소 여부처럼
    **세어야 하는 것**은 코드 것을 쓴다. 판정은 이 결과로만 한다.
    """
    ownership = {**(extracted.get("ownershipInfo") or {}), **derived["ownershipInfo"]}
    return {
        **extracted,
        "ownershipInfo": ownership,
        "mortgageInfo": derived["mortgageInfo"],
        "legalRisks": derived["legalRisks"],
        "otherRights": derived["otherRights"],
        "cancelledRights": derived["cancelledRights"],
    }


# 말소 이력을 붙일 항목. 권리 종류 → 체크리스트 항목 id
_CANCELLED_TO_ITEM = {
    "압류": "seizure",
    "가압류": "provisional_seizure",
    "가처분": "provisional_seizure",
    "처분금지가처분": "provisional_seizure",
    "임의경매개시결정": "auction",
    "강제경매개시결정": "auction",
    "근저당권": "mortgage_scale",
    "신탁": "trust",
    "가등기": "preliminary",
    "전세권": "lease_rights",
    "임차권": "lease_rights",
}


def _note_cancelled(checklist: list, cancelled: list) -> list:
    """
    말소된 과거 이력을 해당 항목의 설명 끝에 붙인다 — "과거 압류 3건은 모두 말소되었습니다."

    **상태는 바꾸지 않는다.** 말소된 권리는 위험이 아니다. 다만 과거 체납·분쟁 이력은 임차인에게 참고가 되고,
    원문에 '압류'가 보이는데 결과가 '없음'이라고만 하면 사용자는 결과를 의심한다.
    이 문장을 서술 모델에 맡기지 않는 이유: 지침을 주어도 빠뜨렸다. 건수는 코드가 센 값이어야 한다.
    """
    counts: dict = {}
    for r in cancelled or []:
        item = _CANCELLED_TO_ITEM.get(r.get("type"))
        if item:
            counts.setdefault(item, {}).setdefault(r["type"], 0)
            counts[item][r["type"]] += 1
    noted = []
    for c in checklist:
        per_type = counts.get(c["id"])
        if per_type:
            parts = ", ".join(f"{t} {n}건" for t, n in per_type.items())
            c = {**c, "detail": f"{c['detail']} 과거 {parts}은(는) 모두 말소되었습니다."}
        noted.append(c)
    return noted


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _clean_narrative(narrative) -> dict:
    """
    서술 호출의 결과를 응답이 기대하는 모양으로 맞춘다.

    구조화 출력을 강제해도 모델은 모양을 가끔 바꾼다 — 객체여야 할 riskSummary 를 문자열로 주는 식이다.
    그대로 쓰면 조립하다 500 이 난다. **뜻이 분명한 것은 살리고**(문자열로 온 요약은 그 요약이다),
    알아볼 수 없는 것은 버린다. 버려진 서술은 조건부 필드의 규칙대로 그 자리를 비운다.
    """
    n = narrative if isinstance(narrative, dict) else {}

    analysis = {}
    raw = n.get("checklistAnalysis")
    for key, value in (raw.items() if isinstance(raw, dict) else []):
        if isinstance(value, dict) and (_text(value.get("findings")) or _text(value.get("leaseImpact"))):
            analysis[key] = {"findings": _text(value.get("findings")) or "", "leaseImpact": _text(value.get("leaseImpact")) or ""}
        elif _text(value):
            analysis[key] = {"findings": _text(value), "leaseImpact": ""}

    risk = n.get("riskSummary")
    risk_content = _text(risk.get("content")) if isinstance(risk, dict) else _text(risk)

    recommendations = [
        {"priority": _text(r.get("priority")) or "참고", "title": _text(r.get("title")), "description": _text(r.get("description")) or ""}
        for r in (n.get("recommendations") if isinstance(n.get("recommendations"), list) else [])
        if isinstance(r, dict) and _text(r.get("title"))
    ]

    return {
        "analysisSummary": _text(n.get("analysisSummary")),
        "overallSummary": _text(n.get("overallSummary")),
        "checklistAnalysis": analysis,
        "riskSummaryContent": risk_content,
        "recommendations": recommendations,
    }


_RISK_LEVEL = {"SAFE": "낮음", "CAUTION": "보통", "DANGER": "높음"}


def _risk_level(safety_level: str) -> str:
    """위험요소 요약의 수준은 등급에서 정한다. 모델이 따로 매기면 등급과 다른 말을 할 수 있다."""
    return _RISK_LEVEL.get(safety_level, "보통")


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
