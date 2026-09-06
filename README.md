# SafeHome AI API

등기부등본 섹션 텍스트를 받아 **임차인 보증금 보호 관점의 위험 분석 결과**를 구조화된 JSON으로 돌려주는 Flask 서비스.
RAG로 관련 법령·전세사기 사례를 찾아 LLM 컨텍스트에 주입하고, 최종 판정은 Python이 결정적으로 계산한다.

> **범위**: `project-safehome-ai-api/**`
> **연관**: [API README](../project-safehome-api/README.md) — 이 서버를 호출하는 쪽 (워크스페이스에 함께 있을 때만 유효한 링크)
> **여기 없는 것**: 함수·상수 이름과 호출 설정값 — 코드가 답한다.
> 계약 절과 판정 규칙은 응답 조립부·판정 코드와 대조해 확인한다.

---

## TL;DR

등기부 섹션 텍스트를 받아 위험 분석 결과를 구조화된 JSON으로 돌려준다.
검색으로 법령·사례를 찾아 모델 컨텍스트에 넣고, **최종 판정은 결정적 코드가 계산한다.**

```bash
pip install -r requirements.txt
cp .env.example .env        # 키 입력
python app.py               # 개발 서버
```

첫 실행에서 지식 데이터를 임베딩하느라 수십 초 걸린다. 이후에는 기존 저장소를 재사용한다.

모델·임베딩·저장소의 구체적인 선택과 호출 설정은 코드와 `requirements.txt`가 답한다.

---

## 설계 원칙 — 역할 분리

이 서비스의 핵심 결정이다. **모델은 사실 추출과 서술만, 판정은 결정적 코드가 한다.**

| 담당 | 하는 일 |
|------|--------|
| **모델** | 등기부에서 사실을 추출하고, 항목별 서술과 권고 문장을 쓴다 |
| **코드** | 추출된 사실로 체크리스트 상태를 계산 → 안전 등급 산출 → 근거 조회 → 응답 조립 |

시스템 프롬프트가 **판정 관련 필드를 응답에 넣지 말라고 모델에게 명시**하고, 코드가 채운다.

이 분리를 택한 근거 → 아래 **결정 이유** 절.

---

## 작업 레시피

파일 이름이 아니라 **순서**가 중요하다. 실제 위치는 코드에서 찾는다.

| 하려는 일 | 순서 |
|-----------|------|
| **응답 필드 추가·변경** | 응답 조립부 → 아래 **API 엔드포인트** 절(계약) → 하류(서버 → 앱) |
| **위험 판정 기준 변경** | 체크리스트 판정 코드 → 필요하면 등급 산출 코드 → **상류 캐시 무효화** |
| **체크리스트 항목 추가** | 시스템 프롬프트의 출력 스키마 → 판정 코드 → 검색 우선순위 → 앱 모델 |
| **프롬프트 수정** | 시스템 프롬프트 → **상류 캐시 무효화** (안 하면 옛 결과가 계속 나간다) |
| **지식 데이터 추가** | [`data/README.md`](data/README.md) 참조 — 등록과 저장소 재생성이 함께 필요 |
| **검색 키워드 조정** | [`rag/README.md`](rag/README.md) 참조 — 재현성에 영향을 준다 |

---

## 구조

```
project-safehome-ai-api/
├── app.py              # Flask 라우트 + 시스템 프롬프트 + 결정적 판정 로직
├── rag/                # → rag/README.md
│   └── updater.py      # 뉴스·법령 자동 수집 → ChromaDB 반영
├── data/               # RAG 데이터셋 JSON → data/README.md
├── chroma_db/          # 벡터 저장소 (자동 생성, 커밋 대상 아님)
├── docker/             # 배포 구성 → docker/README.md
└── Dockerfile          # gunicorn -w 1 --threads 2 --timeout 120
```

---

## API 엔드포인트

| Method | Path | 용도 |
|--------|------|------|
| `GET` | `/` | `{"message": "Hello, World!"}` |
| `GET` | `/health` | `{"status": "ok"}` — 헬스체크 |
| `POST` | `/api/chat` | OpenAI Chat Completions 범용 프록시 (디버깅·실험용) |
| `POST` | `/api/deed/analyze` | **핵심** — 등기부등본 분석 |

### `POST /api/deed/analyze`

**이 절이 계약의 원본이다.** 호출하는 쪽은 여기에 맞춘다.

```jsonc
{ "sections": { "표제부": ["..."], "갑구": ["..."], "을구": ["..."] },
  "leaseType": "전세" }   // 선택: "전세" | "월세". 없으면 "미지정" 취급
```

**응답 — 유효한 등기부인 경우**

```jsonc
{
  "analysis": {
    "isValidDeed": true,
    "safetyLevel": "SAFE | CAUTION | DANGER",   // Python 계산
    "analysisSummary": "2~3문장 핵심 요약",
    "propertyInfo":  { "address", "type", "area", "structure", "purpose", "buildYear" },
    "ownershipInfo": { "currentOwner", "ownerType", "shareRatio", "recentTransferDate",
                       "recentTransferCause", "transferCount", "frequentTransferWarning",
                       "transferHistory": [...] },
    "checklist": [                               // 항상 11개
      { "id": "ownership_clarity", "category": "소유권", "item": "...",
        "status": "양호 | 주의 | 위험", "detail": "...",
        "analysis": { "findings": "...", "leaseImpact": "..." } }
    ],
    "riskSummary":     { "leaseType", "level": "낮음|보통|높음", "content" },
    "overallSummary":  "5~8문장 종합 서술",
    "recommendations": [ { "priority": "필수|권장|참고", "title", "description" } ],
    "references":      { "laws": [...], "cases": [...] }   // safetyLevel != SAFE 일 때만
  },
  "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 }
}
```

> **응답에 없는 것**: `mortgageInfo`, `otherRights`, `legalRisks`는 LLM이 추출하지만 **내부 계산용**이라 응답에서 제외된다. 클라이언트에 노출하려면 응답 조립부를 고치고 계약부터 합의해야 한다.

**응답 — 등기부등본이 아닌 경우** (`checklist` 계산을 건너뛴다)

```jsonc
{ "analysis": { "isValidDeed": false, "reason": "..." }, "usage": { ... } }
```

**에러**: 전역 `@app.errorhandler(Exception)` → `{"error": "..."}` + `500`.
LLM 응답이 토큰 한도로 잘리면(`finish_reason == "length"`) 파싱을 시도하지 않고 즉시 500을 반환한다.

---

## 판정 규칙

### 등급 산출

체크리스트 항목의 상태를 모아 하나의 등급으로 접는다. **가장 나쁜 항목이 전체를 결정한다.**

| 항목 상태 | 등급 |
|---|---|
| 하나라도 위험 | `DANGER` |
| 위험 없이 주의만 있음 | `CAUTION` |
| 전부 양호 | `SAFE` |

### 체크리스트 항목

응답에 나가는 항목의 식별자와 분류다. **앱이 이 값을 받아 표시하므로 계약에 준한다.**

| 분류 | 항목 |
|---|---|
| 소유권 | `ownership_clarity` · `transfer_frequency` |
| 담보권 | `mortgage_scale` · `senior_rights` |
| 법적위험 | `provisional_seizure` · `seizure` · `auction` |
| 특수권리 | `lease_rights` · `trust` · `preliminary` · `surface_rights` |

각 항목을 주의·위험으로 판정하는 **임계값은 코드가 갖는다.** 여기에 복제하면 어긋난다.

### 판정 방식

판정은 모델이 채운 분류 문자열의 **부분 일치**로 이뤄지고, 허용값은 시스템 프롬프트가 고정한다.
임대차 유형에 따라 항목 제목과 설명 문구가 달라지지만, **상태 계산 자체는 동일하다.**

---

## 검색 (RAG)

분석 전에는 컨텍스트를 확보하고, 분석 후에는 판정된 항목에 근거를 붙인다.

두 단계 파이프라인, 지식 저장소의 적재 조건, 실패 처리, **재현성과의 긴장**
→ **[`rag/README.md`](rag/README.md)** · 데이터 형식 → [`data/README.md`](data/README.md)

---

## 환경 변수

| 변수 | 필수 | 기본값 | 설명 |
|------|:---:|--------|------|
| `OPENAI_API_KEY` | ✅ | — | 분석·임베딩 모두 사용 |
| `SENTRY_DSN` | — | 없음 | 미설정 시 Sentry 비활성 |
| `APP_ENV` | — | `local` | Sentry environment 태그 |
| `RAG_UPDATE_HOUR` | — | `3` | 자동 업데이트 실행 시각 (0~23) |
| `LAW_API_KEY` | — | — | 국가법령정보 오픈API 키 |

---

## 배포

기본 브랜치에 push하면 자동으로 이미지를 빌드해 레지스트리에 올리고, 원격 서버에서 컨테이너를 교체한다.
환경별로 이미지 태그와 참조하는 환경 파일이 다르다. 구성과 경로 → [`docker/README.md`](docker/README.md)

**요청 타임아웃을 넉넉히 잡는다.** 모델 응답이 느려서, 기본값으로 두면 정상 분석이 끊긴다.

---

## 결정 이유

저장소 전체에 걸친 선택과 근거. 개별 규칙과 함정은 [`CLAUDE.md`](CLAUDE.md)와 모듈 문서가 갖는다.

**판정을 모델에게 맡기지 않았다.** 같은 등기부에 같은 등급이 나와야 하는데,
모델에 맡기면 재현성이 없고 재현성이 없으면 상류의 캐시 전략이 성립하지 않는다.
그래서 사실 추출과 서술만 모델이 하고, 등급 계산은 결정적 코드가 한다.

**검색을 필수 의존으로 만들지 않았다.** 지식이 없어도 분석은 나와야 한다고 봤다.
품질은 떨어지지만 서비스는 살아 있는 쪽을 택했다.

**토큰 사용량을 응답에 싣지 않는다.** 상류가 분석 결과만 저장하므로 실어도 버려진다.
추적이 필요해지면 상류부터 바꿔야 한다.

---

## 로컬 테스트

```bash
curl http://localhost:5000/health

curl -X POST http://localhost:5000/api/deed/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "sections": {
      "표제부": ["[집합건물] 서울특별시 마포구 망원동 123-4", "전유부분 면적: 59.91㎡"],
      "갑구": ["순위1. 소유권보존 홍길동 2020.03.15"],
      "을구": ["순위1. 근저당권설정 채권최고액 금1억2천만원 국민은행 2020.04.01"]
    },
    "leaseType": "전세"
  }'
```

---

## 문서 지도

| 알고 싶은 것 | 문서 |
|-------------|------|
| RAG 모듈 내부 (loader/retriever 함수·상수) | [`rag/README.md`](rag/README.md) |
| 데이터셋 스키마·추가 방법 | [`data/README.md`](data/README.md) |
| 배포 구성·서버 초기 설정 | [`docker/README.md`](docker/README.md) |
| AI 작업 지침 | [`CLAUDE.md`](CLAUDE.md) |
