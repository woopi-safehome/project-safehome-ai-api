# rag — RAG 시스템

등기부등본 분석 시 관련 법령·사례를 벡터 검색으로 조회하여 LLM 컨텍스트에 주입하는 Retrieval-Augmented Generation 모듈.

## 파일 구조

```
rag/
├── loader.py     # ChromaDB 초기화 + 데이터셋 시딩
└── retriever.py  # 위험 키워드 감지 + 벡터 검색 + 참고문헌 조회
```

## loader.py

### 역할
- ChromaDB PersistentClient 초기화 (`chroma_db/` 디렉토리에 저장)
- 컬렉션이 비어 있으면 `data/`의 JSON 데이터셋을 임베딩하여 적재
- 임베딩 모델: `text-embedding-3-small` (OpenAI)

### 주요 상수

| 상수 | 값 | 설명 |
|------|----|------|
| `_COLLECTION_NAME` | `legal_knowledge` | ChromaDB 컬렉션 이름 |
| `_DATASET_FILES` | `rag_legal_dataset.json`, `rag_news_cases_dataset.json` | 로드할 데이터셋 파일 |
| `_CHROMA_PATH` | `../chroma_db` | 벡터 저장소 경로 |

### 호출 방법

```python
from rag.loader import init_collection

collection = init_collection(api_key=OPENAI_API_KEY)
```

최초 실행 시 전체 임베딩 (수십 초 소요), 이후 실행은 기존 컬렉션 재사용.

---

## retriever.py

### 역할
- 등기부 텍스트에서 위험 키워드(`_RISK_SIGNALS`) 감지
- ChromaDB 코사인 유사도 검색으로 관련 법령·사례 top-N 반환
- 체크리스트 항목별 법령 1건 + 사례 1건 조회 (`retrieve_references`)

### 주요 함수

| 함수 | 설명 |
|------|------|
| `retrieve(collection, sections, n_results=4)` | 분석 전 LLM 컨텍스트 생성용 — 위험 키워드 기반 청크 검색 |
| `retrieve_references(collection, checklist, safety_level)` | 분석 후 참고문헌 조회 — 체크리스트 항목당 법령·사례 1건씩 |

### 위험 키워드 (`_RISK_SIGNALS`)

```
갑구: 신탁, 가등기, 압류, 가압류, 경매, 가처분, 소유권이전, 예고등기, 처분금지, 임의경매, 강제경매, 환매
을구: 근저당, 전세권, 임차권등기, 채권최고액, 지상권, 구분지상권
사기 연관: 법인, 공유, 지분
```

키워드가 하나도 없으면 기본 쿼리(`임차인 보증금 보호 전세사기 위험 대항력 우선변제권`) 사용.

### 위험 우선순위 (`_RISK_PRIORITY`)

```
auction → trust → preliminary → seizure → provisional_seizure
→ mortgage_scale → transfer_frequency → lease_rights
→ senior_rights → ownership_clarity → surface_rights
```

`retrieve_references` 호출 시 이 순서로 정렬하여 가장 치명적인 항목부터 처리.

### RAG 초기화 실패 시

`app.py`에서 `init_collection` 실패를 catch하여 `collection = None`으로 처리.
`retrieve()` 호출 없이 LLM 단독 분석으로 graceful degradation.
