import hashlib
import json
import logging
import os

import chromadb
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_DATASET_FILES = [
    "rag_legal_dataset.json",
    "rag_news_cases_dataset.json",
]
_CHROMA_PATH = os.path.join(os.path.dirname(__file__), "..", "chroma_db")
_COLLECTION_NAME = "legal_knowledge"


def _data_version() -> str:
    """데이터셋 파일 내용의 지문. 한 글자만 바뀌어도 달라진다."""
    h = hashlib.sha256()
    for filename in _DATASET_FILES:
        with open(os.path.join(_DATA_DIR, filename), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def init_collection(api_key: str) -> chromadb.Collection:
    """ChromaDB 컬렉션을 초기화하고 반환.

    **데이터셋이 바뀌었으면 저장소를 지우고 다시 만든다.** 저장소는 비어 있을 때만 채워지므로,
    이 장치가 없으면 데이터를 고쳐도 배포된 서버는 옛 내용(과 자동 수집으로 쌓인 항목)을 계속 쓴다.
    지문은 컬렉션 메타데이터에 남긴다.
    """
    client = chromadb.PersistentClient(path=_CHROMA_PATH)
    ef = OpenAIEmbeddingFunction(api_key=api_key, model_name="text-embedding-3-small")
    version = _data_version()
    metadata = {"hnsw:space": "cosine", "data_version": version}
    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=ef,
        metadata=metadata,
    )
    if (collection.metadata or {}).get("data_version") != version:
        logger.info("RAG 데이터셋이 바뀌어 저장소를 다시 만든다 (%s → %s)",
                    (collection.metadata or {}).get("data_version"), version)
        client.delete_collection(_COLLECTION_NAME)
        collection = client.create_collection(
            name=_COLLECTION_NAME,
            embedding_function=ef,
            metadata=metadata,
        )
    if collection.count() == 0:
        _seed(collection)
        logger.info("RAG 데이터셋 로드 완료: %d개 청크", collection.count())
    else:
        logger.info("RAG 기존 컬렉션 로드: %d개 청크", collection.count())
    return collection


def _seed(collection: chromadb.Collection) -> None:
    """모든 데이터셋 파일을 ChromaDB에 임베딩하여 저장."""
    chunks = []
    for filename in _DATASET_FILES:
        path = os.path.join(_DATA_DIR, filename)
        with open(path, encoding="utf-8") as f:
            chunks.extend(json.load(f))

    collection.add(
        ids=[c["id"] for c in chunks],
        documents=[
            f"{c['title']}\n{c['content']}\n위험 맥락: {c['risk_context']}"
            for c in chunks
        ],
        metadatas=[
            {
                "source": c["source"],
                "article": c["article"],
                "title": c["title"],
                "content": c["content"],
                "risk_context": c["risk_context"],
                "tags": ",".join(c["tags"]),
            }
            for c in chunks
        ],
    )
