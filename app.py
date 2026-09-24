import json
import logging
import os
import sentry_sdk
from datetime import date
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, request
from openai import OpenAI
from dotenv import load_dotenv
from rag import init_collection, retrieve, retrieve_references
from judgment import _build_response, _compute_checklist, _compute_safety_level, _clean_narrative, _derive_facts, _merge_facts, _note_cancelled, _risk_level

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    environment=os.getenv("APP_ENV", "local"),
    traces_sample_rate=0.2,
    send_default_pii=False,
)

app = Flask(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# RAG 컬렉션 초기화 (앱 시작 시 1회)
_rag_collection = None
try:
    _rag_collection = init_collection(os.getenv("OPENAI_API_KEY"))
except Exception as e:
    logger.warning("RAG 초기화 실패(RAG 없이 동작): %s", e, exc_info=True)
    sentry_sdk.capture_exception(e)

# RAG 자동 업데이터 스케줄러
# Flask debug 모드는 reloader가 프로세스를 2개 띄우므로 메인 프로세스에서만 실행
_is_main_process = not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
if _is_main_process:
    def _run_updater():
        try:
            from rag.updater import run as updater_run
            updater_run()
        except Exception as e:
            logger.error("RAG 자동 업데이트 실패: %s", e, exc_info=True)
            sentry_sdk.capture_exception(e)

    _update_hour = int(os.getenv("RAG_UPDATE_HOUR", "3"))
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _run_updater,
        CronTrigger(hour=_update_hour, minute=0),
        id="rag_updater",
        max_instances=1,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("RAG 자동 업데이터 스케줄 등록 완료 (매일 %02d:00)", _update_hour)

# ─────────────────────────────────────────────────────────────
# 시스템 프롬프트 — 호출이 둘이다
#
#   1. 추출: 모델이 등기 항목을 순위번호·등기목적·날짜·금액 그대로 베껴 적는다. 판단하지 않는다.
#   2. (코드) 말소를 짝지어 지우고, 건수·합계·이전 횟수를 세고, 항목 상태와 등급을 정한다.
#   3. 서술: 모델이 **코드가 확정한 사실만** 받아 요약·설명·권고를 쓴다. 원문을 다시 주지 않는다.
#
# 서술 호출에 원문을 주지 않는 이유: 원문을 보면 모델이 말소된 권리를 살아 있는 것처럼 다시 쓴다.
# 확정된 사실만 주면 틀린 말을 할 근거가 없다. 배경: README.md 의 "설계 원칙 — 역할 분리"
# ─────────────────────────────────────────────────────────────
EXTRACT_SYSTEM_PROMPT = """당신은 대한민국 부동산 등기부등본을 옮겨 적는 사람입니다.
판단하거나 요약하지 말고, 등기부에 적힌 항목을 빠짐없이 그대로 옮기세요.
반드시 아래 JSON 스키마 형식으로만 응답하고, JSON 외의 텍스트는 포함하지 마세요.

═══════════════════════════════════════
[입력 유효성 검사 — 최우선]
═══════════════════════════════════════
입력이 대한민국 부동산 등기부등본이 아니면(표제부·갑구·을구 구조가 없거나 부동산과 무관하면) 아래만 응답하세요:
{ "isValidDeed": false, "reason": "등기부등본이 아닌 이유를 한 문장으로" }

═══════════════════════════════════════
[옮겨 적는 규칙 — 가장 중요]
═══════════════════════════════════════
1. 갑구와 을구의 **모든 순위번호 항목**을 순서대로 entries 에 넣습니다. 하나도 빼지 마세요.
2. **말소된 항목도, 말소하는 항목도 모두 넣습니다.** PDF 에서는 말소 표시(취소선)가 보이지 않습니다.
   "3번압류등기말소", "1번근저당권설정, 2번근저당권설정등기말소" 같은 항목이 앞 항목을 지웁니다.
   어느 것이 지워졌는지는 판단하지 말고, 이런 항목을 적힌 그대로 옮기기만 하세요.
3. 부기등기(순위번호 "1-1", "2-3" 등)와 "N번근저당권변경", "N번근저당권이전" 도 그대로 넣습니다.
4. purpose 는 등기목적 칸의 글자를 **그대로** 적습니다. 바꿔 쓰거나 줄이지 마세요.
   등기목적이 두 줄로 나뉘어 있으면(예: "3번근저당권설정등" 다음 줄 "기말소") **이어 붙여 한 칸에** 적습니다.
5. 확인할 수 없는 값은 null 로 둡니다.

═══════════════════════════════════════
[응답 JSON 스키마 — 유효한 등기부인 경우]
═══════════════════════════════════════
{
  "isValidDeed": true,
  "propertyInfo": {
    "address": "소재지 전체 주소",
    "type": "부동산 종류 (토지/건물/집합건물/구분건물)",
    "area": "면적 (㎡ 및 평 환산 포함, 예: 59.91㎡ / 약 18.1평)",
    "structure": "구조 (철근콘크리트/목조 등)",
    "purpose": "주 용도 (아파트/단독주택/다세대/상가/사무실 등)",
    "buildYear": "건축연도 (확인 불가 시 null)"
  },
  "ownershipInfo": {
    "currentOwner": "현재 소유자명",
    "ownerType": "단독소유 또는 공유",
    "shareRatio": "공유 시 해당 지분 비율 (예: 1/2), 단독 시 null"
  },
  "entries": [
    {
      "section": "갑구 또는 을구",
      "rank": "순위번호 그대로 (예: \\"3\\", \\"1-3\\")",
      "purpose": "등기목적 그대로 (예: \\"압류\\", \\"3번압류등기말소\\", \\"근저당권설정\\", \\"소유권이전\\")",
      "date": "접수일 (YYYY-MM-DD)",
      "cause": "등기원인 그대로 (예: \\"2013년10월25일 매매\\", \\"2019년1월2일 해제\\")",
      "holder": "권리자 (소유자·근저당권자·채권자·권리자 이름)",
      "amount": "채권최고액·청구금액·전세금 등 금액 그대로 (없으면 null)"
    }
  ]
}"""

NARRATIVE_SYSTEM_PROMPT = """당신은 대한민국 부동산 등기부등본 분석 전문가입니다.
이 서비스의 목적은 전세·월세 계약을 앞둔 임차인이 사기 피해를 입지 않도록 위험 신호를 알기 쉽게 알려 주는 것입니다.
반드시 아래 JSON 스키마 형식으로만 응답하고, JSON 외의 텍스트는 포함하지 마세요.

═══════════════════════════════════════
[서술 원칙 — 가장 중요]
═══════════════════════════════════════
1. 사용자 메시지의 [확정된 사실]만 근거로 씁니다. **등급과 항목 상태는 이미 정해져 있습니다.** 바꾸거나 다르게 평가하지 마세요.
2. 확정된 사실에 없는 권리를 있다고 쓰지 마세요.
3. cancelledRights 는 **이미 말소된 과거 이력**입니다. 현재 위험처럼 쓰지 마세요.
   다만 그 권리에 해당하는 항목(압류 → seizure, 가압류·가처분 → provisional_seizure, 근저당 → mortgage_scale 등)의 findings 에는
   "과거 N건 있었으나 모두 말소되었다"처럼 **반드시 함께 적습니다.** 임차인에게는 과거 체납·분쟁 이력도 참고가 됩니다.
4. 건수와 금액은 확정된 사실의 숫자를 그대로 씁니다. 다시 세거나 더하지 마세요.
5. leaseType(전세/월세/미지정)에 맞는 관점으로 씁니다.
6. 참고 자료(법령·사례)가 주어지면 서술의 근거로 활용합니다.

═══════════════════════════════════════
[응답 JSON 스키마]
═══════════════════════════════════════
{
  "analysisSummary": "2~3문장. 가장 중요한 위험 신호 또는 안전 사유를 핵심만.",

  "checklistAnalysis": {
    "ownership_clarity": { "findings": "소유권 구조 사실", "leaseImpact": "임차인 보증금에 미치는 영향" },
    "transfer_frequency": { "findings": "소유권 이전 이력 사실", "leaseImpact": "갭투자·전세사기 위험과의 관계" },
    "mortgage_scale": { "findings": "활성 근저당 건수·채권최고액 합계·채권자", "leaseImpact": "담보 규모가 보증금 회수에 미치는 영향" },
    "senior_rights": { "findings": "선순위 근저당·전세권·임차권 사실", "leaseImpact": "임차인이 후순위가 될 때의 영향" },
    "provisional_seizure": { "findings": "가압류·가처분 사실 (말소된 과거 이력이 있으면 함께)", "leaseImpact": "채무 분쟁이 보증금 반환에 미치는 위험" },
    "seizure": { "findings": "압류 사실 (말소된 과거 이력이 있으면 함께)", "leaseImpact": "세금 체납으로 인한 국가 우선 변제 위험" },
    "auction": { "findings": "경매개시결정 사실", "leaseImpact": "경매 진행 시 보증금 손실 가능성" },
    "lease_rights": { "findings": "선순위 전세권·임차권 등기 사실", "leaseImpact": "기존 임차인 우선 변제 위험" },
    "trust": { "findings": "신탁등기 사실", "leaseImpact": "수탁자 동의 없는 임대차의 대항력 위험" },
    "preliminary": { "findings": "가등기 사실", "leaseImpact": "본등기 시 임차권 소멸 위험" },
    "surface_rights": { "findings": "지상권·구분지상권 사실", "leaseImpact": "임차 생활의 제한" }
  },

  "riskSummary": {
    "content": "3~5문장. 전세라면 보증금 전액 보호 가능성, 월세라면 소액 보증금 보호와 최우선변제권 중심으로."
  },

  "overallSummary": "5~8문장. 부동산 기본 정보 → 소유권 → 담보·권리 부담 → 법적 위험 → 보증금 안전성 순서로.",

  "recommendations": [
    { "priority": "필수 또는 권장 또는 참고", "title": "확인 사항 제목 (간결하게)", "description": "구체적인 확인 방법과 주의사항" }
  ]
}

═══════════════════════════════════════
[임대차 유형별 recommendations 작성 지침]
═══════════════════════════════════════

■ leaseType = "전세" 인 경우 recommendations에 반드시 포함해야 할 항목:
1. [필수] 전입신고 + 확정일자 — 계약 당일 전입신고 및 확정일자 부여로 대항력·우선변제권 확보.
2. [필수] 전세보증보험 가입 — HUG(주택도시보증공사) 또는 SGI서울보증 전세보증보험 가입 가능 여부 및 조건 확인.
3. [필수] 선순위 채권 합산 검토 — 근저당 채권최고액 + 전세보증금 합계가 시세의 70~80%를 초과하면 계약 재고.
4. [필수] 계약 당일 등기부 재확인 — 계약서 작성·잔금 지급 직전에 등기부등본을 다시 발급하여 변동 여부 확인.
5. [권장] 전세가율 확인 — 시세 대비 전세보증금 비율 80% 이하인지 확인. 갭투자 위험 판단 기준.
6. [권장] 전세권 설정등기 — 전세금을 지급하기 전 전세권 설정등기를 통해 대항력 강화.
7. [권장] 임대인 세금 완납 확인 — 국세·지방세 완납증명서 제출 요청. 체납 시 국가 우선 변제.

■ leaseType = "월세" 인 경우 recommendations에 반드시 포함해야 할 항목:
1. [필수] 전입신고 + 확정일자 — 소액 보증금도 반드시 전입신고 및 확정일자 부여. 최우선변제권 적용 기준 충족 여부 확인.
2. [필수] 소액임차인 최우선변제권 확인 — 지역별 최우선변제 기준(서울 5,500만 원 이하 등) 충족 여부와 배당 가능 금액 확인.
3. [필수] 임대인 실소유자 확인 — 등기부상 소유자와 계약 당사자 일치 여부, 대리인 계약 시 위임장·인감증명서 필수.
4. [필수] 계약 당일 등기부 재확인 — 계약서 작성·보증금 지급 직전 등기부등본을 다시 발급하여 압류·가압류 변동 확인.
5. [권장] 신탁등기 시 수탁자 동의 — 신탁등기가 있는 경우 수탁자(신탁회사)의 임대 동의서 수령 필수.
6. [참고] 임대차 계약서 보관 — 확정일자 받은 계약서 원본 보관. 계약 갱신 시에도 재확인 필요."""


@app.route("/")
def hello_world():
    return jsonify({"message": "Hello, World!"})


@app.route("/health")
def health_check():
    return jsonify({"status": "ok"})


@app.route("/api/chat", methods=["POST"])
def chat():
    """
    OpenAI Chat Completions API 호출
    Request body:
      - messages: list of {role, content}  (required)
      - model: string                       (optional, default: gpt-4o-mini)
      - temperature: float                  (optional, default: 1.0)
      - max_tokens: int                     (optional)
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Request body must be JSON"}), 400

    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        return jsonify({"error": "'messages' field is required and must be a list"}), 400

    model = body.get("model", "gpt-4o-mini")
    temperature = body.get("temperature", 1.0)
    max_tokens = body.get("max_tokens")

    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    response = client.chat.completions.create(**kwargs)

    choice = response.choices[0]
    return jsonify({
        "id": response.id,
        "model": response.model,
        "message": {
            "role": choice.message.role,
            "content": choice.message.content,
        },
        "finish_reason": choice.finish_reason,
        "usage": {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        },
    })


@app.route("/api/deed/analyze", methods=["POST"])
def analyze_deed():
    """
    등기부등본 섹션 분석 API
    Request body:
      - sections: Map<String, List<String>>  (required)
      - leaseType: "전세" | "월세"           (optional)
    Response:
      - analysis: 구조화된 분석 결과 (JSON object)

    추출 → (코드) 판정 → 서술. 흐름과 이유는 시스템 프롬프트 위의 설명을 본다.
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Request body must be JSON"}), 400

    sections = body.get("sections")
    if not sections or not isinstance(sections, dict):
        return jsonify({"error": "'sections' field is required and must be an object"}), 400

    lease_type = body.get("leaseType")  # "월세" | "전세" | None
    sentry_sdk.set_tag("lease_type", lease_type or "미지정")

    # 1. 추출 — 판단 없이 옮겨 적기
    extract_response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": _build_extract_prompt(sections)},
        ],
        temperature=0,
        seed=42,
        max_tokens=8000,
        response_format={"type": "json_object"},
    )
    extracted = _parse_json(extract_response, "추출")
    usage = _usage(extract_response)

    if not extracted.get("isValidDeed", False):
        return jsonify({"analysis": {"isValidDeed": False, "reason": extracted.get("reason")}, "usage": usage})

    # 2. 판정 — 말소를 지우고 세는 것부터 등급까지 코드가 한다
    facts = _merge_facts(extracted, _derive_facts(extracted.get("entries"), date.today()))
    checklist = _compute_checklist(facts, {}, lease_type)
    safety_level = _compute_safety_level(checklist)

    rag_context = ""
    if _rag_collection is not None:
        try:
            rag_context = retrieve(_rag_collection, sections, lease_type=lease_type)
        except Exception as e:
            logger.warning("RAG 검색 실패 (RAG 없이 분석): %s", e, exc_info=True)
            sentry_sdk.capture_exception(e)
    sentry_sdk.set_tag("rag_used", _rag_collection is not None and bool(rag_context))

    # 3. 서술 — 확정된 사실만 준다
    narrative_response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": NARRATIVE_SYSTEM_PROMPT},
            {"role": "user", "content": _build_narrative_prompt(facts, checklist, safety_level, lease_type, rag_context)},
        ],
        temperature=0,
        seed=42,
        max_tokens=6000,
        response_format={"type": "json_object"},
    )
    narrative = _clean_narrative(_parse_json(narrative_response, "서술"))
    usage = {k: usage[k] + v for k, v in _usage(narrative_response).items()}

    # 서술을 항목에 붙인다. 상태는 이미 정해졌고 서술은 바꾸지 못한다.
    checklist = _note_cancelled(_compute_checklist(facts, narrative["checklistAnalysis"], lease_type), facts["cancelledRights"])

    # CAUTION / DANGER 일 때만 위험·주의 항목별로 법령·사례 검색 후 중복 제거
    references = {}
    if safety_level != "SAFE" and _rag_collection is not None:
        try:
            references = retrieve_references(_rag_collection, checklist, safety_level)
        except Exception as e:
            logger.warning("참조 법령/사례 검색 실패 (생략): %s", e, exc_info=True)
            sentry_sdk.capture_exception(e)

    risk_summary = {
        "leaseType": lease_type or "미지정",
        "level": _risk_level(safety_level),
        "content": narrative["riskSummaryContent"],
    }
    final_analysis = _build_response(
        {**facts, **narrative, "riskSummary": risk_summary}, checklist, safety_level, references,
    )

    logger.info(
        "분석 완료 — leaseType=%s, safetyLevel=%s, entries=%d, tokens=%d",
        lease_type or "미지정", safety_level, len(extracted.get("entries") or []), usage["total_tokens"],
    )
    return jsonify({"analysis": final_analysis, "usage": usage})


def _parse_json(response, stage: str) -> dict:
    """모델 응답을 JSON 으로 읽는다. 잘렸거나 깨졌으면 조용히 넘기지 않고 실패시킨다."""
    choice = response.choices[0]
    if choice.finish_reason == "length":
        logger.error("LLM %s 응답이 토큰 한도로 잘림, 응답 길이=%d", stage, len(choice.message.content or ""))
        raise ValueError(f"LLM {stage} 응답이 너무 길어 처리할 수 없습니다.")
    try:
        return json.loads(choice.message.content)
    except json.JSONDecodeError as e:
        logger.error("LLM %s 응답 JSON 파싱 실패: %s | 응답 길이=%d", stage, e, len(choice.message.content or ""))
        raise


def _usage(response) -> dict:
    return {
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
    }


def _build_extract_prompt(sections: dict) -> str:
    parts = []
    for section_name, lines in sections.items():
        content = "\n".join(lines) if isinstance(lines, list) else str(lines)
        parts.append(f"[{section_name}]\n{content}")
    return "다음 등기부등본의 항목을 옮겨 적어 주세요:\n\n" + "\n\n".join(parts)


def _build_narrative_prompt(facts: dict, checklist: list, safety_level: str, lease_type: str, rag_context: str) -> str:
    """서술 호출에 줄 확정된 사실. 원문은 넣지 않는다 — 말소된 권리를 다시 살려 쓰지 않게."""
    confirmed = {
        "leaseType": lease_type or "미지정",
        "safetyLevel": safety_level,
        "propertyInfo": facts.get("propertyInfo"),
        "ownershipInfo": facts.get("ownershipInfo"),
        "activeMortgages": facts.get("mortgageInfo"),
        "activeLegalRisks": facts.get("legalRisks"),
        "activeOtherRights": facts.get("otherRights"),
        "cancelledRights": facts.get("cancelledRights"),
        "checklist": [{"id": c["id"], "item": c["item"], "status": c["status"], "detail": c["detail"]} for c in checklist],
    }
    prompt = "[확정된 사실]\n" + json.dumps(confirmed, ensure_ascii=False, indent=1)
    if rag_context:
        prompt = f"[관련 법령 및 위험 패턴 참고 자료]\n{rag_context}\n\n" + prompt
    return prompt + "\n\n위 확정된 사실만 근거로 서술해 주세요."


@app.errorhandler(Exception)
def handle_exception(e):
    sentry_sdk.capture_exception(e)
    return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
