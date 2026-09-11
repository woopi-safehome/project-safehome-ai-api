import json
import logging
import os
import sentry_sdk
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, request
from openai import OpenAI
from dotenv import load_dotenv
from rag import init_collection, retrieve, retrieve_references
from judgment import _compute_checklist, _compute_safety_level, _build_response

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
# 시스템 프롬프트
# LLM 역할: 사실 추출 + 유형별 서술 생성
# Python 역할: checklist status 판정 + safetyLevel 계산 (결정적)
# ─────────────────────────────────────────────────────────────
DEED_SYSTEM_PROMPT = """당신은 대한민국 부동산 등기부등본 분석 전문가입니다.
이 서비스의 목적은 전세·월세 계약을 앞둔 임차인이 사기 피해를 입지 않도록, 등기부등본에서 위험 신호를 사전에 발견하는 것입니다.
임차인의 보증금 보호 관점에서 빠짐없이 분석하고, 반드시 아래 JSON 스키마 형식으로만 응답하세요.
JSON 외의 다른 텍스트는 절대 포함하지 마세요.

[임대차 유형]
사용자 메시지에 leaseType이 명시된 경우 해당 유형(월세/전세)에 맞는 분석을 수행하세요.
leaseType이 없거나 "미지정"인 경우 전세·월세 공통 관점으로 분석하세요.

═══════════════════════════════════════
[입력 유효성 검사 - 최우선 처리]
═══════════════════════════════════════
입력된 내용이 대한민국 부동산 등기부등본이 아닌 경우 (표제부·갑구·을구 구조가 없거나, 부동산과 무관한 내용인 경우),
아래 형식으로만 응답하고 다른 분석은 수행하지 마세요:
{
  "isValidDeed": false,
  "reason": "등기부등본이 아닌 이유를 한 문장으로 설명"
}

═══════════════════════════════════════
[분석 원칙]
═══════════════════════════════════════
1. 등기부에 기재된 사실을 정확하게 추출하는 것이 최우선이다.
2. 확인할 수 없는 항목은 null로 표기한다.
3. 모든 금액은 원화(원) 단위로 표기한다.
4. legalRisks와 otherRights의 type 필드는 아래 허용값 중 하나만 사용한다.

═══════════════════════════════════════
[legalRisks.type 허용값 — 정확히 일치해야 함]
═══════════════════════════════════════
가압류 / 압류 / 가처분 / 처분금지가처분 / 임의경매개시결정 / 강제경매개시결정 / 예고등기

═══════════════════════════════════════
[otherRights.type 허용값 — 정확히 일치해야 함]
═══════════════════════════════════════
전세권 / 임차권 / 지상권 / 구분지상권 / 지역권 / 신탁 / 가등기 / 환매특약

═══════════════════════════════════════
[유효한 등기부등본인 경우 응답 JSON 스키마]
═══════════════════════════════════════
{
  "isValidDeed": true,

  "propertyInfo": {
    "address": "소재지 전체 주소",
    "type": "부동산 종류 (토지/건물/집합건물/구분건물)",
    "area": "면적 (㎡ 및 평 환산 포함, 예: 59.91㎡ / 약 18.1평)",
    "structure": "구조 (철근콘크리트/목조 등)",
    "purpose": "주 용도 (아파트/단독주택/다세대/상가/사무실 등)",
    "buildYear": "건축연도 (확인 가능 시, 불가 시 null)"
  },

  "ownershipInfo": {
    "currentOwner": "현재 소유자명",
    "ownerType": "단독소유 또는 공유",
    "shareRatio": "공유 시 해당 지분 비율 (예: 1/2), 단독 시 null",
    "recentTransferDate": "가장 최근 소유권 취득일 (YYYY-MM-DD)",
    "recentTransferCause": "가장 최근 취득 원인 (매매/상속/증여/경매/판결 등)",
    "transferCount": 0,
    "frequentTransferWarning": false,
    "transferHistory": [
      {
        "owner": "소유자명",
        "acquisitionDate": "취득일 (YYYY-MM-DD 또는 확인불가)",
        "acquisitionCause": "취득원인",
        "isCurrent": false
      }
    ]
  },

  "mortgageInfo": {
    "totalCount": 0,
    "activeCount": 0,
    "totalMaxClaimAmount": "활성 근저당 합산 채권최고액 (없으면 '없음')",
    "riskComment": "담보 현황에 대한 임차인 관점 위험 코멘트 (안전하면 안전 사유 명시)",
    "details": [
      {
        "rank": 1,
        "type": "근저당권 또는 저당권",
        "creditor": "채권자 (금융기관명 또는 개인)",
        "maxClaimAmount": "채권최고액",
        "registrationDate": "설정일 (YYYY-MM-DD)",
        "isActive": true,
        "note": "특이사항 (공동담보/채권양도/일부말소 등, 없으면 null)"
      }
    ]
  },

  "otherRights": [
    {
      "type": "위 허용값 중 하나",
      "holder": "권리자",
      "amount": "금액 (있는 경우, 없으면 null)",
      "period": "존속기간 (있는 경우, 없으면 null)",
      "registrationDate": "등기일 (YYYY-MM-DD)",
      "tenantImpact": "임차인 보증금에 미치는 영향 설명"
    }
  ],

  "legalRisks": [
    {
      "type": "위 허용값 중 하나",
      "claimant": "청구인 또는 압류기관 (국세청/지자체/금융기관/개인 등)",
      "amount": "청구금액 또는 압류금액 (있는 경우, 없으면 null)",
      "registrationDate": "등기일 (YYYY-MM-DD)",
      "severity": "HIGH 또는 MEDIUM 또는 LOW",
      "description": "위험 내용과 임차인 보증금에 미치는 구체적 영향 설명"
    }
  ],

  "analysisSummary": "2~3문장. 이 등기부등본에서 발견된 가장 중요한 위험 신호 또는 안전 사유를 핵심만 요약.",

  "checklistAnalysis": {
    "ownership_clarity": {
      "findings": "소유권 구조에 대한 등기부 사실 기술 (단독/공유, 지분 비율 등)",
      "leaseImpact": "leaseType 관점에서 이 소유권 구조가 임차인 보증금에 미치는 영향"
    },
    "transfer_frequency": {
      "findings": "소유권 이전 이력에 대한 사실 기술 (이전 횟수, 시기, 원인)",
      "leaseImpact": "leaseType 관점에서 이전 이력이 갭투자·전세사기 위험과 어떻게 연결되는지"
    },
    "mortgage_scale": {
      "findings": "근저당 설정 현황 사실 기술 (건수, 채권최고액 합산, 채권자 등)",
      "leaseImpact": "leaseType 관점에서 담보 규모가 보증금 회수에 미치는 영향 (전세: 보증금 전액 위험, 월세: 경매 시 퇴거·소액 보증금 위험)"
    },
    "senior_rights": {
      "findings": "선순위 근저당·전세권·임차권 존재 여부 사실 기술",
      "leaseImpact": "leaseType 관점에서 선순위 권리로 인해 임차인이 후순위가 될 경우의 영향"
    },
    "provisional_seizure": {
      "findings": "가압류·가처분 등기 존재 여부 및 내용 사실 기술",
      "leaseImpact": "leaseType 관점에서 채무 분쟁이 보증금 반환에 미치는 위험"
    },
    "seizure": {
      "findings": "압류 등기 존재 여부 및 압류 주체(국세청/지자체 등) 사실 기술",
      "leaseImpact": "leaseType 관점에서 세금 체납으로 인한 국가 우선 변제 위험"
    },
    "auction": {
      "findings": "경매개시결정 등기 존재 여부 및 경매 유형 사실 기술",
      "leaseImpact": "leaseType 관점에서 경매 진행 시 보증금 전액 손실 가능성"
    },
    "lease_rights": {
      "findings": "선순위 전세권·임차권 등기 존재 여부 및 금액 사실 기술",
      "leaseImpact": "leaseType 관점에서 기존 임차인 보증금이 신규 임차인보다 우선 변제되는 위험"
    },
    "trust": {
      "findings": "신탁등기 존재 여부 및 수탁자 정보 사실 기술",
      "leaseImpact": "leaseType 관점에서 수탁자 동의 없는 임대차 계약의 대항력 위험"
    },
    "preliminary": {
      "findings": "가등기 존재 여부 및 종류(소유권이전청구권 가등기 여부) 사실 기술",
      "leaseImpact": "leaseType 관점에서 본등기 완료 시 임차권 소멸 위험"
    },
    "surface_rights": {
      "findings": "지상권·구분지상권 설정 여부 및 내용 사실 기술",
      "leaseImpact": "leaseType 관점에서 지상권이 임차 생활에 미치는 제한"
    }
  },

  "riskSummary": {
    "leaseType": "전세 또는 월세 또는 미지정",
    "level": "낮음 또는 보통 또는 높음",
    "content": "3~5문장. 위험 요소를 종합하여 leaseType 관점에서 보증금 안전성을 평가. 전세라면 보증금 전액 보호 가능성 중심, 월세라면 소액 보증금 보호 및 최우선변제권 적용 여부 중심으로 서술."
  },

  "overallSummary": "5~8문장. 부동산 기본 정보 → 소유권 현황 → 담보·권리 부담 → 법적 위험 → 임차인 보증금 안전성 평가 순으로 서술.",

  "recommendations": [
    {
      "priority": "필수 또는 권장 또는 참고",
      "title": "확인 사항 제목 (간결하게)",
      "description": "구체적인 확인 방법과 주의사항"
    }
  ]
}

※ checklist와 safetyLevel 필드는 응답에 포함하지 마세요. 시스템이 자동 계산합니다.

frequentTransferWarning: 3년 이내 소유권 이전 2회 이상이면 true (전세사기 갭투자 패턴 주의)

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
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "Request body must be JSON"}), 400

    sections = body.get("sections")
    if not sections or not isinstance(sections, dict):
        return jsonify({"error": "'sections' field is required and must be an object"}), 400

    lease_type = body.get("leaseType")  # "월세" | "전세" | None

    rag_context = ""
    if _rag_collection is not None:
        try:
            rag_context = retrieve(_rag_collection, sections, lease_type=lease_type)
        except Exception as e:
            logger.warning("RAG 검색 실패 (RAG 없이 분석): %s", e, exc_info=True)
            sentry_sdk.capture_exception(e)

    user_prompt = _build_user_prompt(sections, rag_context, lease_type)

    sentry_sdk.set_tag("lease_type", lease_type or "미지정")
    sentry_sdk.set_tag("rag_used", _rag_collection is not None and bool(rag_context))
    logger.info("분석 시작 — leaseType=%s, rag_used=%s", lease_type or "미지정", bool(rag_context))

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": DEED_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        seed=42,
        max_tokens=6000,
        response_format={"type": "json_object"},
    )

    finish_reason = response.choices[0].finish_reason
    raw_content = response.choices[0].message.content

    if finish_reason == "length":
        logger.error("LLM 응답이 토큰 한도 초과로 잘림 (finish_reason=length), 응답 길이=%d", len(raw_content))
        return jsonify({"error": "LLM 응답이 너무 길어 처리할 수 없습니다."}), 500

    try:
        llm_output = json.loads(raw_content)
    except json.JSONDecodeError as e:
        logger.error("LLM 응답 JSON 파싱 실패: %s | 응답 길이=%d | finish_reason=%s", e, len(raw_content), finish_reason)
        raise

    if not llm_output.get("isValidDeed", False):
        return jsonify({
            "analysis": llm_output,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
        })

    # checklist status 결정적 계산 (LLM 사실 데이터 + LLM 서술 병합)
    checklist_analysis = llm_output.get("checklistAnalysis") or {}
    checklist = _compute_checklist(llm_output, checklist_analysis, lease_type)
    safety_level = _compute_safety_level(checklist)

    # CAUTION / DANGER 일 때만 위험·주의 항목별로 법령·사례 검색 후 중복 제거
    references = {}
    if safety_level != "SAFE" and _rag_collection is not None:
        try:
            references = retrieve_references(_rag_collection, checklist, safety_level)
        except Exception as e:
            logger.warning("참조 법령/사례 검색 실패 (생략): %s", e, exc_info=True)
            sentry_sdk.capture_exception(e)

    final_analysis = _build_response(llm_output, checklist, safety_level, references)

    logger.info(
        "분석 완료 — leaseType=%s, safetyLevel=%s, tokens=%d",
        lease_type or "미지정",
        safety_level,
        response.usage.total_tokens,
    )

    return jsonify({
        "analysis": final_analysis,
        "usage": {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        },
    })


# ─────────────────────────────────────────────────────────────
# 결정적 계산 함수들
# ─────────────────────────────────────────────────────────────







def _build_user_prompt(sections: dict, rag_context: str = "", lease_type: str = None) -> str:
    parts = []
    for section_name, lines in sections.items():
        content = "\n".join(lines) if isinstance(lines, list) else str(lines)
        parts.append(f"[{section_name}]\n{content}")
    sections_text = "\n\n".join(parts)

    lease_line = f"[임대차 유형]\nleaseType: {lease_type}\n\n" if lease_type else ""

    if rag_context:
        return (
            f"[관련 법령 및 위험 패턴 참고 자료]\n{rag_context}\n\n"
            f"{lease_line}"
            f"위 참고 자료를 바탕으로 다음 등기부등본 데이터를 분석해주세요:\n\n{sections_text}"
        )
    return f"{lease_line}다음 등기부등본 데이터를 분석해주세요:\n\n{sections_text}"


@app.errorhandler(Exception)
def handle_exception(e):
    sentry_sdk.capture_exception(e)
    return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
