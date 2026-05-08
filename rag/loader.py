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


def init_collection(api_key: str) -> chromadb.Collection:
    """ChromaDB 컬렉션을 초기화하고 반환. 데이터가 없으면 데이터셋을 로드."""
    client = chromadb.PersistentClient(path=_CHROMA_PATH)
    ef = OpenAIEmbeddingFunction(api_key=api_key, model_name="text-embedding-3-small")
    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
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
                "tags": ",".join(c["tags"]),
            }
            for c in chunks
        ],
    )
