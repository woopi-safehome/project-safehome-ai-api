# SafeHome AI API

등기부등본 섹션 텍스트를 받아 **임차인 보증금 보호 관점의 위험 분석 결과**를 구조화된 JSON으로 돌려주는 Flask 서비스.
RAG로 관련 법령·전세사기 사례를 찾아 LLM 컨텍스트에 주입하고, 최종 판정은 Python이 결정적으로 계산한다.

> **범위**: `project-safehome-ai-api/**`
> **연관**: [루트 README](../README.md) (API→AI API 계약) · [API README](../project-safehome-api/README.md)
> **검증**: 응답 스키마는 `app.py`의 `_build_response()`, 판정 규칙은 `_compute_checklist()` / `_compute_safety_level()`과 대조

---

## TL;DR

| 항목 | 값 |
|------|-----|
| 스택 | Python 3.11 / Flask 3.1.0 / gunicorn |
| LLM | OpenAI `gpt-4o-mini` — `temperature=0`, `seed=42`, `max_tokens=6000`, `response_format=json_object` |
| 임베딩 | `text-embedding-3-small` |
| 벡터 DB | ChromaDB (PersistentClient, cosine), 컬렉션 `legal_knowledge` |
| 진입점 | `app.py` (라우트 + 시스템 프롬프트 + 판정 로직) |
| 호출자 | `project-safehome-api`의 `LlmAnalysisAdapter` |

```bash
pip install -r requirements.txt
cp .env.example .env        # OPENAI_API_KEY 입력
python app.py               # :5000 (개발 서버)
```

첫 실행 시 ChromaDB에 데이터셋 36개 청크를 임베딩한다 (수십 초). 이후 실행은 기존 컬렉션을 재사용한다.

---

## 설계 원칙 — 역할 분리

이 서비스의 핵심 결정이다. **LLM은 사실 추출과 서술만, 판정은 Python이 한다.**

| 담당 | 하는 일 |
|------|--------|
| **LLM** | 등기부에서 사실 추출 (`ownershipInfo`, `mortgageInfo`, `otherRights`, `legalRisks`) + 항목별 서술 (`checklistAnalysis`, `riskSummary`, `overallSummary`, `recommendations`) |
| **Python** | 추출된 사실로 체크리스트 11항목의 `status` 계산 → `safetyLevel` 계산 → 참조 법령·사례 조회 → 응답 조립 |

**왜**: 같은 등기부에 같은 등급이 나와야 하는데 LLM에 등급 판정을 맡기면 재현성이 없다.
그래서 시스템 프롬프트가 `checklist`·`safetyLevel` 필드를 **응답에 넣지 말라고 명시**하고, Python이 채운다.

---

## 작업 레시피

| 하려는 일 | 건드릴 파일 (순서대로) |
|-----------|----------------------|
| **응답 필드 추가/변경** | `app.py` `_build_response()` → 루트 README의 API→AI API 계약 → App `lib/models/deed.dart` → `build_runner` 재실행 |
| **위험 판정 기준 변경** | `app.py` `_compute_checklist()` (해당 `_item` 블록) → 필요 시 `_compute_safety_level()` → 아래 판정 규칙 표 |
| **체크리스트 항목 추가** | `DEED_SYSTEM_PROMPT`의 `checklistAnalysis` 스키마 → `_compute_checklist()` 리스트 → `rag/retriever.py`의 `_RISK_PRIORITY` → App 모델 |
| **프롬프트 수정** | `DEED_SYSTEM_PROMPT` → **API의 캐시 무효화 필요** (`LlmCacheAdapter.KEY_PREFIX` 버전 업) |
| **RAG 데이터 추가** | `data/`에 JSON 추가 → `rag/loader.py` `_DATASET_FILES` 등록 → `chroma_db/` 삭제 후 재기동 → [`data/README.md`](data/README.md) |
| **검색 키워드 조정** | `rag/retriever.py` `_RISK_SIGNALS` → [`rag/README.md`](rag/README.md) |

---

## 구조

```
project-safehome-ai-api/
├── app.py              # Flask 라우트 + 시스템 프롬프트 + 결정적 판정 로직
├── rag/                # → rag/README.md
│   ├── loader.py       # ChromaDB 초기화 + 데이터셋 시딩
│   ├── retriever.py    # 위험 키워드 감지 + 벡터 검색 + 참조 조회
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

요청 형식은 [루트 README의 계약](../README.md#-api--ai-api)이 원본이다.

```jsonc
{ "sections": { "표제부": ["..."], "갑구": ["..."], "을구": ["..."] },
  "leaseType": "전세" }   // 선택: "전세" | "월세". 없으면 "미지정" 취급
```

**응답 — 유효한 등기부인 경우** (`_build_response()` 조립 결과)

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

> **응답에 없는 것**: `mortgageInfo`, `otherRights`, `legalRisks`는 LLM이 추출하지만 **내부 계산용**이라 응답에서 제외된다. 클라이언트에 노출하려면 `_build_response()`를 수정해야 한다.

**응답 — 등기부등본이 아닌 경우** (`checklist` 계산을 건너뛴다)

```jsonc
{ "analysis": { "isValidDeed": false, "reason": "..." }, "usage": { ... } }
```

**에러**: 전역 `@app.errorhandler(Exception)` → `{"error": "..."}` + `500`.
LLM 응답이 토큰 한도로 잘리면(`finish_reason == "length"`) 파싱을 시도하지 않고 즉시 500을 반환한다.

---

## 판정 규칙

### safetyLevel (`_compute_safety_level`)

| 조건 | 등급 |
|------|------|
| 11개 항목 중 `위험`이 하나라도 있음 | `DANGER` |
| `위험` 없고 `주의`가 있음 | `CAUTION` |
| 전부 `양호` | `SAFE` |

### 체크리스트 11항목 (`_compute_checklist`)

| id | 분류 | `주의` 조건 | `위험` 조건 |
|----|------|------------|------------|
| `ownership_clarity` | 소유권 | 공유지분 | — |
| `transfer_frequency` | 소유권 | 이전 이력 1회 이상 | `frequentTransferWarning`(3년 내 2회+) |
| `mortgage_scale` | 담보권 | 활성 근저당 1~2건 | 활성 근저당 3건 이상 |
| `senior_rights` | 담보권 | 활성 근저당 또는 전세권·임차권 존재 | — |
| `provisional_seizure` | 법적위험 | — | 가압류·가처분·처분금지 |
| `seizure` | 법적위험 | — | 압류 (가압류 제외) |
| `auction` | 법적위험 | — | 경매개시결정 |
| `lease_rights` | 특수권리 | 선순위 전세권·임차권 | — |
| `trust` | 특수권리 | — | 신탁등기 |
| `preliminary` | 특수권리 | — | 가등기 |
| `surface_rights` | 특수권리 | 지상권·구분지상권 | — |

판정은 LLM이 채운 `legalRisks[].type` / `otherRights[].type` 문자열의 **부분 일치**로 한다.
그래서 시스템 프롬프트가 두 필드의 **허용값을 고정**한다. 허용값을 바꾸면 판정이 조용히 깨지므로 프롬프트와 `_compute_checklist()`를 반드시 함께 수정한다.

`leaseType`이 `월세`면 항목 제목과 `detail` 문구가 월세 관점으로 바뀐다 (`is_wolse` 분기). status 계산 자체는 동일하다.

---

## RAG

```
앱 시작 → init_collection()
   ├ chroma_db/ 비어 있음 → 데이터셋 36개 청크 임베딩 저장
   └ 이미 있음 → 기존 벡터 로드 (OpenAI 호출 없음)

분석 요청
   ├ [분석 전] retrieve()            → 위험 키워드 감지 → 유사 청크 top-4 → 프롬프트에 주입
   └ [분석 후] retrieve_references() → safetyLevel != SAFE 일 때만, 위험/주의 항목당
                                       법령 1건 + 사례 1건 검색 후 중복 제거 → references
```

| 데이터셋 | 청크 | 내용 |
|---------|:---:|------|
| `rag_legal_dataset.json` | 20 | 주택임대차보호법, 민법, 민사집행법, 전세사기 특별법 등 |
| `rag_news_cases_dataset.json` | 16 | 실제 전세사기 사례 분석 |

**graceful degradation**: `init_collection` 또는 `retrieve` 가 실패해도 예외를 삼키고 RAG 없이 LLM 단독으로 분석을 계속한다. RAG는 품질 향상 수단이지 필수 의존이 아니다.

상세 → [`rag/README.md`](rag/README.md) · 데이터 스키마 → [`data/README.md`](data/README.md)

### 자동 업데이터

APScheduler가 매일 `RAG_UPDATE_HOUR`시(기본 3시)에 `rag/updater.py`의 `run()`을 실행해 Google News RSS와 국가법령정보 API에서 수집한 내용을 ChromaDB에 반영한다.

```bash
python rag/updater.py            # 수동 실행 (최근 1일치)
python rag/updater.py --days 3   # 최근 3일치
```

로그: `logs/update_YYYYMMDD.log`
Flask `debug=True`는 reloader가 프로세스를 2개 띄우므로 `WERKZEUG_RUN_MAIN` 체크로 메인 프로세스에서만 스케줄러를 등록한다.

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

`develop` push → GitHub Actions → `ghcr.io` → SSH → `docker compose up -d`.
워크플로우: `.github/workflows/deploy-ai-api-dev.yml` / `deploy-ai-api-prd.yml`

| 환경 | 이미지 태그 | 컴포즈 |
|------|-----------|--------|
| dev | `dev`, `dev-{short sha}` | `docker/dev/` |
| prd | `latest`, `{git tag}` | `docker/prd/` |

서버 환경변수 파일: `/home/woopi/project/safehome/env/.env_ai_api`
상세 → [`docker/README.md`](docker/README.md)

**운영 실행은 gunicorn**(`-w 1 --threads 2 --timeout 120`)이다. 워커가 1개인 이유는 ChromaDB PersistentClient가 프로세스별로 저장소를 잡기 때문이다. LLM 응답이 느려 타임아웃은 120초로 잡혀 있다.

---

## 함정 & 결정 이유

| 함정 | 내용 |
|------|------|
| **프롬프트 수정 = 캐시 무효화** | API가 분석 결과를 Redis에 7일 캐싱한다. 프롬프트·판정 로직을 바꾸면 옛 결과가 계속 나간다. `LlmCacheAdapter.KEY_PREFIX`를 `v3`로 올려야 한다 |
| **`type` 허용값 고정** | `legalRisks.type` / `otherRights.type`은 프롬프트에 열거된 값만 나와야 한다. 판정이 문자열 부분 일치라 값이 바뀌면 조용히 `양호`로 떨어진다 |
| **`chroma_db/` 삭제 없이는 데이터 추가 안 됨** | `collection.count() == 0`일 때만 시딩한다. 데이터셋을 추가했으면 디렉토리를 지우고 재기동해야 한다 |
| **`_seed`는 필수 키를 가정한다** | 청크에 `id/title/content/risk_context/source/article/tags`가 없으면 `KeyError`로 초기화가 통째로 실패한다 (→ RAG 없이 동작) |
| **`id` 중복 금지** | 데이터셋 전체에서 고유해야 한다. 중복 시 ChromaDB 오류 |
| **`temperature=0` + `seed=42`** | 같은 입력에 같은 결과를 내기 위한 설정이다. 재현성이 캐시 전략의 전제이므로 바꾸지 말 것 |
| **`usage`는 버려진다** | API가 `analysis`만 저장한다. 토큰 사용량을 추적하려면 API 쪽도 함께 고쳐야 한다 |

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
