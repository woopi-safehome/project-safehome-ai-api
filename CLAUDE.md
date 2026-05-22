# CLAUDE.md — project-safehome-ai-api

## 빌드 & 실행

```bash
pip install -r requirements.txt
cp .env.example .env   # OPENAI_API_KEY 입력
python app.py          # http://localhost:5000
```

## 기술 스택

Python 3.11+, Flask 3.1.0, OpenAI API (gpt-4o-mini), ChromaDB 1.x, python-dotenv

## 구조

```
app.py          # Flask 진입점 (라우트 + 시스템 프롬프트)
rag/
  loader.py     # ChromaDB 초기화 + 데이터셋 시딩
  retriever.py  # 위험 키워드 감지 + 벡터 검색
data/           # RAG 데이터셋 JSON
chroma_db/      # 벡터 저장소 (자동 생성)
```

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | `/health` | 헬스 체크 |
| POST | `/api/deed/analyze` | 등기부등본 분석 (핵심) |

**POST /api/deed/analyze** 요청:
```json
{
  "sections": { "표제부": [...], "갑구": [...], "을구": [...] },
  "leaseType": "전세 | 월세 (optional)"
}
```

응답: `{ "analysis": { "isValidDeed", "safetyLevel", "propertyInfo", ... } }`
유효하지 않은 입력: `{ "analysis": { "isValidDeed": false, "reason": "..." } }`

## 핵심 패턴

- 분석 모델: `gpt-4o-mini`, `temperature=0.2`, `response_format={"type": "json_object"}`
- RAG: 키워드 감지 → ChromaDB 벡터 검색(top-4) → 컨텍스트 주입
- RAG 초기화 실패 시 graceful degradation (LLM 단독 분석으로 동작)
- 에러: `@app.errorhandler(Exception)` 전역 핸들러 → 500 반환

## RAG 데이터셋 확장

`data/`에 JSON 추가 후 `rag/loader.py`의 `DATA_FILES` 목록에 등록.
청크 스키마: `{ "source": "...", "article": "...", "content": "..." }`

## 환경 변수

| 변수 | 설명 | 필수 |
|------|------|------|
| `OPENAI_API_KEY` | OpenAI API 키 | 필수 |
