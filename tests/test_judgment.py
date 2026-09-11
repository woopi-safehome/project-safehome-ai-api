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
