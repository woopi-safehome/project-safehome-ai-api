# data — RAG 데이터셋

ChromaDB에 임베딩되는 법령·사례 JSON 파일 모음.
`rag/loader.py`의 `_DATASET_FILES`에 등록된 파일만 로드된다.

## 파일 목록

| 파일 | 건수 | 출처 유형 |
|------|:---:|---------|
| `rag_legal_dataset.json` | 20건 | 주택임대차보호법, 민법 등 법령 조문 |
| `rag_news_cases_dataset.json` | 16건 | 실제 전세사기 사례 분석 |

## 청크 스키마

### rag_legal_dataset.json

```json
{
  "id": "unique_chunk_id",
  "source": "주택임대차보호법",
  "article": "제3조 (대항력)",
  "title": "대항력 요건",
  "content": "법령 본문 또는 해설",
  "keywords": ["대항력", "전입신고", "점유"],
  "risk_context": "이 조항이 위험 판단에 연관되는 맥락 설명",
  "tags": ["대항력", "보증금보호"]
}
```

### rag_news_cases_dataset.json

```json
{
  "id": "unique_chunk_id",
  "source": "실제 사례 분석",
  "article": "빌라왕 무자본 갭투자 사태 (2022~2023)",
  "title": "빌라왕 갭투자 전세사기 패턴",
  "content": "사례 상세 내용",
  "keywords": ["갭투자", "무자본", "돌려막기"],
  "risk_context": "이 사례가 위험 판단에 연관되는 맥락",
  "damage_scale": "피해 규모 (사례 파일에만 있음)",
  "tags": ["갭투자", "전세사기"]
}
```

> `damage_scale` 필드는 사례 파일에만 존재하며 법령 파일에는 없다.

## 새 데이터 추가 방법

1. 위 스키마에 맞는 JSON 파일을 `data/`에 추가
2. `rag/loader.py`의 `_DATASET_FILES` 리스트에 파일명 등록

```python
_DATASET_FILES = [
    "rag_legal_dataset.json",
    "rag_news_cases_dataset.json",
    "rag_new_dataset.json",   # 추가
]
```

3. `chroma_db/` 디렉토리 삭제 후 재실행 → 전체 재임베딩

```bash
rm -rf chroma_db/
python app.py
```

> `chroma_db/`를 삭제하지 않으면 기존 컬렉션이 비어있지 않다고 판단해 새 데이터를 로드하지 않는다.

## 주의사항

- `id` 필드는 전체 청크 내 고유해야 한다 (중복 시 ChromaDB 오류)
- `tags` 필드는 쉼표 없는 문자열 리스트 (`["태그1", "태그2"]`)
- `source` 값이 `"실제 사례 분석"`이면 `retriever.py`에서 사례(case)로 분류됨
