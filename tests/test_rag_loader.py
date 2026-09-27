"""지식 저장소는 데이터셋이 바뀌면 다시 만들어진다.

저장소는 비어 있을 때만 채워진다. 데이터셋을 고쳐도 배포된 서버의 저장소(도커 볼륨)가 남아 있으면
옛 내용이 계속 나간다 — 그래서 데이터 지문이 달라지면 지우고 다시 만든다. 외부 호출 없이 돌도록
임베딩은 가짜로 바꾼다.
"""
import json
import shutil
from pathlib import Path

import pytest
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

import rag.loader as loader


class _FakeEmbedding(EmbeddingFunction[Documents]):
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, input: Documents) -> Embeddings:
        return [[float(len(t) % 7), 1.0, 0.5] for t in input]

    @staticmethod
    def name() -> str:
        return "fake"

    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        return _FakeEmbedding()


@pytest.fixture
def store(tmp_path, monkeypatch):
    data = tmp_path / "data"
    shutil.copytree(Path(loader._DATA_DIR), data)
    monkeypatch.setattr(loader, "_DATA_DIR", str(data))
    monkeypatch.setattr(loader, "_CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setattr(loader, "OpenAIEmbeddingFunction", _FakeEmbedding)
    return data


def _titles(collection):
    return {m["title"] for m in collection.get(include=["metadatas"])["metadatas"]}


def test_same_data_keeps_the_store(store):
    first = loader.init_collection("unused")
    # 자동 수집 등으로 들어간 항목 — 데이터가 그대로면 지우지 않는다.
    first.add(ids=["extra"], documents=["추가"], metadatas=[{"title": "추가 항목"}])
    second = loader.init_collection("unused")
    assert "추가 항목" in _titles(second), "데이터셋이 그대로인데 저장소를 다시 만들었다"


def test_changed_data_rebuilds_the_store(store):
    first = loader.init_collection("unused")
    first.add(ids=["extra"], documents=["추가"], metadatas=[{"title": "추가 항목"}])

    path = store / "rag_legal_dataset.json"
    items = json.loads(path.read_text(encoding="utf-8"))
    items[0]["content"] = "고친 내용"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")

    second = loader.init_collection("unused")
    titles = _titles(second)
    assert "추가 항목" not in titles, (
        "데이터셋이 바뀌었는데 저장소를 다시 만들지 않았다 — 옛 내용이 배포 서버에 계속 남는다 (rag/README 불변식)"
    )
    contents = {m["content"] for m in second.get(include=["metadatas"])["metadatas"]}
    assert "고친 내용" in contents
