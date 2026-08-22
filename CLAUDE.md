# CLAUDE.md — project-safehome-ai-api

**이 모듈 작업 시의 금지사항·필수 절차.**
스택·구조·실행 명령·응답 스키마·판정 규칙 → [`README.md`](README.md)

---

## 시작 전

1. [루트 `README.md`](../README.md) — API→AI API 계약
2. [`README.md`](README.md) — **설계 원칙(역할 분리)** 과 **작업 레시피** 표
3. `rag/README.md` · `data/README.md`

## 필수 절차

- **판정은 Python이, 서술은 LLM이 한다.** 안전 등급·체크리스트 status를 LLM에게 맡기지 않는다. 재현성이 캐시 전략의 전제다.
- **프롬프트나 판정 로직을 바꾸면 API 쪽 캐시를 무효화해야 한다** — `LlmCacheAdapter.KEY_PREFIX` 버전 업. 안 하면 옛 결과가 7일간 계속 나간다.
- **`legalRisks.type` / `otherRights.type` 허용값을 바꿀 때는** `DEED_SYSTEM_PROMPT`와 `_compute_checklist()`를 **반드시 함께** 고친다. 판정이 문자열 부분 일치라 한쪽만 고치면 조용히 `양호`로 떨어진다.
- **응답 필드를 추가하면** `_build_response()` → 루트 README 계약 → App `lib/models/deed.dart` → `build_runner` 순으로 전파한다.
- **데이터셋을 추가하면** `rag/loader.py`의 `_DATASET_FILES`에 등록하고 `chroma_db/`를 삭제 후 재기동한다. 컬렉션이 비어 있을 때만 시딩한다.

## 금지사항

- **`temperature=0` / `seed=42`를 바꾸지 않는다.** 같은 입력에 같은 결과가 나와야 한다.
- **RAG 실패를 치명적으로 처리하지 않는다.** `init_collection` / `retrieve` 예외는 잡아서 로그만 남기고 LLM 단독 분석으로 진행한다 (graceful degradation).
- **`mortgageInfo` / `otherRights` / `legalRisks`를 응답에 넣지 않는다.** 내부 계산용이다. 노출이 필요하면 계약부터 합의한다.
- **gunicorn 워커를 2 이상으로 늘리지 않는다.** ChromaDB PersistentClient가 프로세스별로 저장소를 잡는다.
- **`chroma_db/`를 커밋하지 않는다.** 자동 생성물이다.
- `OPENAI_API_KEY` 등 시크릿을 코드·문서에 하드코딩하지 않는다.
