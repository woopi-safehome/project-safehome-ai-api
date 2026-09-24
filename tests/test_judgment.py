"""
판정 로직의 동작을 고정한다.

이 서비스의 제품 가치가 여기에 있다. 모델은 사실 추출과 서술만 하고
등급은 이 코드가 결정적으로 계산한다 - 같은 입력에 같은 등급이 나와야
상류의 캐시 전략이 성립하기 때문이다.

배경: README.md 의 "설계 원칙 — 역할 분리" 와 "판정 규칙"
"""

from judgment import _build_response, _compute_checklist, _compute_safety_level


def _checklist(**llm):
    return _compute_checklist(llm, {}, None)


def _status_of(items, item_id):
    return next(i["status"] for i in items if i["id"] == item_id)


# ── 등급 산출 — 가장 나쁜 항목이 전체를 결정한다 ────────────────────────────

def _items(*statuses):
    return [{"status": s} for s in statuses]


def test_전부_양호면_안전이다():
    assert _compute_safety_level(_items("양호", "양호", "양호")) == "SAFE"


def test_주의가_하나라도_있으면_주의다():
    assert _compute_safety_level(_items("양호", "주의", "양호")) == "CAUTION"


def test_위험이_하나라도_있으면_위험이다():
    assert _compute_safety_level(_items("양호", "양호", "위험")) == "DANGER"


def test_위험과_주의가_섞이면_위험이_이긴다():
    # 가장 나쁜 항목이 전체를 결정한다. 개수로 다수결하지 않는다.
    assert _compute_safety_level(_items("위험", "주의", "주의", "주의")) == "DANGER"


# ── 체크리스트 — 항목 수는 계약이다 ─────────────────────────────────────────

def test_입력이_비어도_열한_항목이_모두_나온다():
    items = _checklist()
    assert len(items) == 11
    assert {i["status"] for i in items} == {"양호"}


def test_입력이_무엇이든_항목_수는_변하지_않는다():
    # 앱이 이 개수에 맞춘다. 위험이 있든 없든 항목은 빠지지 않는다.
    risky = _checklist(
        legalRisks=[{"type": "임의경매"}, {"type": "압류"}],
        otherRights=[{"type": "신탁"}, {"type": "전세권"}],
        mortgageInfo={"activeCount": 3},
        ownershipInfo={"ownerType": "공유"},
    )
    assert len(risky) == 11
    assert [i["id"] for i in risky] == [i["id"] for i in _checklist()]


# ── 사실 → 상태 ─────────────────────────────────────────────────────────────

def test_공유지분이면_소유권_항목이_주의가_된다():
    items = _checklist(ownershipInfo={"ownerType": "공유지분"})
    assert _status_of(items, "ownership_clarity") == "주의"


def test_경매가_있으면_경매_항목이_위험이_된다():
    items = _checklist(legalRisks=[{"type": "임의경매"}])
    assert _status_of(items, "auction") == "위험"


def test_신탁등기가_있으면_신탁_항목이_움직인다():
    기본 = _status_of(_checklist(), "trust")
    신탁 = _status_of(_checklist(otherRights=[{"type": "신탁등기"}]), "trust")
    assert 신탁 != 기본


def test_압류와_가압류를_구분한다():
    # 판정이 부분 일치라 "가압류"는 "압류"를 포함한다.
    # 구분이 무너지면 가압류만 있는 등기부가 압류로도 잡혀 등급이 부풀려진다.
    가압류만 = _checklist(legalRisks=[{"type": "가압류"}])
    assert _status_of(가압류만, "provisional_seizure") != "양호"
    assert _status_of(가압류만, "seizure") == "양호"

    압류만 = _checklist(legalRisks=[{"type": "압류"}])
    assert _status_of(압류만, "seizure") != "양호"


def test_근저당_건수가_선순위_판단에_들어간다():
    없음 = _status_of(_checklist(), "senior_rights")
    있음 = _status_of(_checklist(mortgageInfo={"activeCount": 2}), "senior_rights")
    assert 있음 != 없음


# ── 임대차 유형 ─────────────────────────────────────────────────────────────

def test_임대차_유형이_달라도_상태_계산은_같다():
    # 유형에 따라 항목 제목과 설명 문구는 달라지지만 상태는 같아야 한다.
    사실 = {
        "legalRisks": [{"type": "압류"}],
        "mortgageInfo": {"activeCount": 2},
        "ownershipInfo": {"ownerType": "공유"},
    }
    전세 = _compute_checklist(사실, {}, "전세")
    월세 = _compute_checklist(사실, {}, "월세")

    assert [i["status"] for i in 전세] == [i["status"] for i in 월세]
    assert [i["id"] for i in 전세] == [i["id"] for i in 월세]


# ── 모델 서술의 병합 — 조건부 필드다 ────────────────────────────────────────

def test_모델_서술이_없으면_analysis_가_붙지_않는다():
    # 계약: checklist[].analysis 는 모델이 채웠을 때만 붙는다.
    for item in _compute_checklist({}, {}, None):
        assert "analysis" not in item


def test_모델_서술이_있으면_그_항목에만_붙는다():
    서술 = {"ownership_clarity": {"findings": "단독소유", "leaseImpact": "영향 없음"}}
    items = _compute_checklist({}, 서술, None)

    붙은것 = [i["id"] for i in items if "analysis" in i]
    assert 붙은것 == ["ownership_clarity"]
    assert items[0]["analysis"]["findings"] == "단독소유"


# ── 응답 조립 ───────────────────────────────────────────────────────────────

def test_내부_계산용_사실은_응답에_실리지_않는다():
    응답 = _build_response(
        {
            "mortgageInfo": {"activeCount": 1},
            "otherRights": [{"type": "신탁"}],
            "legalRisks": [{"type": "압류"}],
            "analysisSummary": "요약",
        },
        _checklist(),
        "SAFE",
        {},
    )
    assert not ({"mortgageInfo", "otherRights", "legalRisks"} & set(응답))
    assert 응답["analysisSummary"] == "요약"


# ── 등기 항목 → 사실 — 말소와 건수·합계는 코드가 센다 ─────────────────────────
# 모델이 이 짝짓기와 합산을 자주 틀렸다. 실제 등기부(모두 말소된 압류 3건, 활성 근저당 3건)를
# 압류 '위험', 근저당 6건·1.5억으로 읽었다. 아래 항목 목록은 그 등기부의 구조를 그대로 옮겼다.

from datetime import date

from judgment import _derive_facts, _merge_facts, _risk_level, _won

_TODAY = date(2026, 9, 23)

_SAMPLE_ENTRIES = [
    {"section": "갑구", "rank": "1", "purpose": "소유권이전", "cause": "2001년3월2일 매매", "holder": "김갑"},
    {"section": "갑구", "rank": "2", "purpose": "소유권이전", "cause": "2013년10월25일 매매", "holder": "이을"},
    {"section": "갑구", "rank": "3", "purpose": "압류", "holder": "국"},
    {"section": "갑구", "rank": "4", "purpose": "3번압류등기말소", "cause": "해제"},
    {"section": "갑구", "rank": "5", "purpose": "압류", "holder": "구"},
    {"section": "갑구", "rank": "6", "purpose": "압류", "holder": "구"},
    {"section": "갑구", "rank": "7", "purpose": "5번압류등기말소", "cause": "해제"},
    {"section": "갑구", "rank": "8", "purpose": "6번압류등기말소", "cause": "해제"},
    {"section": "을구", "rank": "1", "purpose": "근저당권설정", "amount": "금26,000,000원"},
    {"section": "을구", "rank": "1-3", "purpose": "1번근저당권변경"},
    {"section": "을구", "rank": "2", "purpose": "근저당권설정", "amount": "금26,000,000원정"},
    {"section": "을구", "rank": "3", "purpose": "근저당권설정", "amount": "금19,500,000원"},
    {"section": "을구", "rank": "4", "purpose": "근저당권설정", "amount": "금41,600,000원"},
    {"section": "을구", "rank": "5", "purpose": "1번근저당권설정, 2번근저당권설정등기말소"},
    {"section": "을구", "rank": "6", "purpose": "3번근저당권설정등기말소"},
    {"section": "을구", "rank": "7", "purpose": "근저당권설정", "amount": "금138,960,000원"},
    {"section": "을구", "rank": "8", "purpose": "4번근저당권설정등기말소"},
    {"section": "을구", "rank": "9", "purpose": "근저당권설정", "amount": "금24,000,000원"},
    {"section": "을구", "rank": "10", "purpose": "근저당권설정", "amount": "금64,800,000원"},
    {"section": "을구", "rank": "11", "purpose": "10번근저당권설정등기말소"},
    {"section": "을구", "rank": "12", "purpose": "근저당권설정", "amount": "금150,000,000원"},
]


def test_말소된_압류는_위험으로_잡지_않는다():
    facts = _derive_facts(_SAMPLE_ENTRIES, _TODAY)
    assert facts["legalRisks"] == []
    assert _status_of(_checklist(**_merge_facts({}, facts)), "seizure") == "양호"
    # 사라진 것이 아니라 과거 이력으로 남는다 — 서술이 "모두 해제됐다"고 말할 수 있게
    assert [r["rank"] for r in facts["cancelledRights"] if r["type"] == "압류"] == ["3", "5", "6"]


def test_살아_있는_근저당만_세고_합친다():
    mortgage = _derive_facts(_SAMPLE_ENTRIES, _TODAY)["mortgageInfo"]
    assert [m["rank"] for m in mortgage["details"]] == ["7", "9", "12"]
    assert mortgage["activeCount"] == 3
    assert mortgage["totalMaxClaimAmount"] == "금312,960,000원"


def test_한_항목이_여러_순위를_말소할_수_있다():
    # "1번근저당권설정, 2번근저당권설정등기말소" 는 두 건을 지운다
    facts = _derive_facts(_SAMPLE_ENTRIES, _TODAY)
    지운것 = {r["rank"] for r in facts["cancelledRights"] if r["type"] == "근저당권"}
    assert {"1", "2"} <= 지운것


def test_말소는_같은_구역_안에서만_짝짓는다():
    # 을구의 "3번…말소" 가 갑구 3번 압류를 지우면 안 된다
    entries = [
        {"section": "갑구", "rank": "3", "purpose": "압류"},
        {"section": "을구", "rank": "3", "purpose": "근저당권설정", "amount": "금1원"},
        {"section": "을구", "rank": "4", "purpose": "3번근저당권설정등기말소"},
    ]
    facts = _derive_facts(entries, _TODAY)
    assert [r["type"] for r in facts["legalRisks"]] == ["압류"]
    assert facts["mortgageInfo"]["activeCount"] == 0


def test_부기_항목은_새_권리로_세지_않는다():
    facts = _derive_facts(
        [{"section": "을구", "rank": "1", "purpose": "근저당권설정", "amount": "금1억원"},
         {"section": "을구", "rank": "1-1", "purpose": "1번근저당권이전"},
         {"section": "을구", "rank": "1-2", "purpose": "1번근저당권변경"}],
        _TODAY,
    )
    assert facts["mortgageInfo"]["activeCount"] == 1


def test_금액은_숫자와_한글_단위를_모두_읽는다():
    assert _won("금138,960,000원") == 138_960_000
    assert _won("금26,000,000원정") == 26_000_000
    assert _won("금1억2천만원") == 120_000_000
    assert _won(None) is None


def test_금액을_읽지_못한_근저당은_따로_센다():
    # 합계가 실제보다 적을 수 있다는 사실을 숨기지 않는다
    mortgage = _derive_facts(
        [{"section": "을구", "rank": "1", "purpose": "근저당권설정", "amount": "금1억원"},
         {"section": "을구", "rank": "2", "purpose": "근저당권설정"}],
        _TODAY,
    )["mortgageInfo"]
    assert mortgage["activeCount"] == 2
    assert mortgage["unreadableAmountCount"] == 1


def test_소유권_이전_이력은_코드가_센다():
    ownership = _derive_facts(_SAMPLE_ENTRIES, _TODAY)["ownershipInfo"]
    assert ownership["transferCount"] == 2
    assert ownership["recentTransferDate"] == "2013-10-25"
    assert ownership["recentTransferCause"] == "매매"
    assert ownership["frequentTransferWarning"] is False
    assert [h["isCurrent"] for h in ownership["transferHistory"]] == [False, True]


def test_3년_안에_두_번_이전되면_잦은_이전이다():
    entries = [
        {"section": "갑구", "rank": "1", "purpose": "소유권이전", "cause": "2024년5월1일 매매"},
        {"section": "갑구", "rank": "2", "purpose": "소유권이전", "cause": "2025년8월1일 매매"},
    ]
    assert _derive_facts(entries, _TODAY)["ownershipInfo"]["frequentTransferWarning"] is True
    assert _derive_facts(entries, date(2032, 1, 1))["ownershipInfo"]["frequentTransferWarning"] is False


def test_2월_29일에도_3년_계산이_깨지지_않는다():
    _derive_facts(_SAMPLE_ENTRIES, date(2028, 2, 29))


def test_말소된_신탁과_가등기도_빠진다():
    facts = _derive_facts(
        [{"section": "갑구", "rank": "1", "purpose": "소유권이전청구권가등기"},
         {"section": "갑구", "rank": "2", "purpose": "신탁"},
         {"section": "갑구", "rank": "3", "purpose": "1번가등기말소"},
         {"section": "갑구", "rank": "4", "purpose": "2번신탁등기말소"}],
        _TODAY,
    )
    assert facts["otherRights"] == []


def test_모델이_읽은_소유자는_살리고_센_값만_덮는다():
    merged = _merge_facts(
        {"ownershipInfo": {"currentOwner": "이을", "ownerType": "단독소유", "transferCount": 9}},
        _derive_facts(_SAMPLE_ENTRIES, _TODAY),
    )
    assert merged["ownershipInfo"]["currentOwner"] == "이을"
    assert merged["ownershipInfo"]["transferCount"] == 2


def test_위험요소_요약의_수준은_등급을_따른다():
    assert [_risk_level(s) for s in ("SAFE", "CAUTION", "DANGER")] == ["낮음", "보통", "높음"]


def test_두_줄로_갈라진_말소도_알아본다():
    # 등기목적 칸이 두 줄이면 PDF 글자 순서가 섞여 "…등기말소" 의 뒷부분이 원인 칸으로 넘어온다.
    # 실제 등기부의 추출 결과를 그대로 옮겼다. 목적만 보면 이미 지워진 근저당 5건을 살아 있는 것으로 센다.
    entries = [
        {"section": "을구", "rank": "1", "purpose": "근저당권설정", "amount": "금26,000,000원"},
        {"section": "을구", "rank": "2", "purpose": "근저당권설정", "amount": "금26,000,000원정"},
        {"section": "을구", "rank": "3", "purpose": "근저당권설정", "amount": "금19,500,000원"},
        {"section": "을구", "rank": "5", "purpose": "1번근저당권설정, 2번근저당권설정", "cause": "2000년6월23일 해지 등기말소"},
        {"section": "을구", "rank": "6", "purpose": "3번근저당권설정등", "cause": "2000년6월23일 기말소"},
        {"section": "을구", "rank": "7", "purpose": "근저당권설정", "amount": "금138,960,000원"},
    ]
    mortgage = _derive_facts(entries, _TODAY)["mortgageInfo"]
    assert [m["rank"] for m in mortgage["details"]] == ["7"]


def test_원인_칸의_날짜_숫자를_순위로_읽지_않는다():
    # "2000년6월23일" 의 6·23 을 순위번호로 오인하면 엉뚱한 권리가 지워진다
    entries = [
        {"section": "을구", "rank": "6", "purpose": "근저당권설정", "amount": "금1원"},
        {"section": "을구", "rank": "23", "purpose": "근저당권설정", "amount": "금1원"},
        {"section": "을구", "rank": "30", "purpose": "1번근저당권설정등", "cause": "2000년6월23일 기말소"},
    ]
    assert _derive_facts(entries, _TODAY)["mortgageInfo"]["activeCount"] == 2


# ── 서술 결과의 모양 — 모델은 가끔 모양을 바꾼다 ────────────────────────────

from judgment import _clean_narrative


def test_문자열로_온_위험요소_요약은_그_요약으로_살린다():
    # 실제로 riskSummary 를 객체 대신 문자열로 받아 조립하다 500 이 났다
    assert _clean_narrative({"riskSummary": "보증금 회수가 어렵습니다."})["riskSummaryContent"] == "보증금 회수가 어렵습니다."
    assert _clean_narrative({"riskSummary": {"content": "요약"}})["riskSummaryContent"] == "요약"


def test_알아볼_수_없는_서술은_버리고_깨지지_않는다():
    cleaned = _clean_narrative({
        "checklistAnalysis": {"seizure": "압류 없음", "auction": 3, "trust": {"findings": "", "leaseImpact": ""}},
        "recommendations": ["문자열", {"title": ""}, {"title": "등기부 재확인", "priority": "필수"}],
        "analysisSummary": 42,
    })
    assert cleaned["checklistAnalysis"] == {"seizure": {"findings": "압류 없음", "leaseImpact": ""}}
    assert [r["title"] for r in cleaned["recommendations"]] == ["등기부 재확인"]
    assert cleaned["analysisSummary"] is None
    assert _clean_narrative("전혀 다른 것")["recommendations"] == []


from judgment import _note_cancelled


def test_말소된_이력은_상태를_바꾸지_않고_설명에만_붙는다():
    facts = _merge_facts({}, _derive_facts(_SAMPLE_ENTRIES, _TODAY))
    items = _note_cancelled(_checklist(**facts), facts["cancelledRights"])
    seizure = next(i for i in items if i["id"] == "seizure")
    assert seizure["status"] == "양호"
    assert seizure["detail"].endswith("과거 압류 3건은(는) 모두 말소되었습니다.")
    mortgage = next(i for i in items if i["id"] == "mortgage_scale")
    assert "과거 근저당권 5건" in mortgage["detail"]


def test_말소_이력이_없으면_설명이_그대로다():
    before = _checklist()
    assert _note_cancelled(before, []) == before
