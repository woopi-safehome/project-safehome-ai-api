"""지식 저장소는 데이터셋이 바뀌면 다시 만들어진다.

저장소는 비어 있을 때만 채워진다. 데이터셋을 고쳐도 배포된 서버의 저장소(도커 볼륨)가 남아 있으면
옛 내용이 계속 나간다 — 그래서 데이터 지문이 달라지면 지우고 다시 만든다.

CI 의 테스트 단계에는 chromadb 가 없다. 판단 로직(rag/data_version.py)은 chromadb 없이 늘 검사하고,
실제 저장소를 만들고 지우는 확인은 chromadb 가 있는 곳(로컬)에서만 돈다.
"""
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# rag 패키지를 import 하면 __init__ 이 chromadb 를 불러온다. 판단 모듈만 파일에서 직접 읽는다.
_spec = importlib.util.spec_from_file_location("data_version", ROOT / "rag" / "data_version.py")
dv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dv)


def _copy_data(tmp_path):
    data = tmp_path / "data"
    shutil.copytree(ROOT / "data", data)
    files = sorted(p.name for p in data.glob("*.json"))
    assert files, "데이터셋 파일을 하나도 찾지 못했다 — data/ 경로가 바뀌었는지 본다"
    return data, files


def test_version_changes_when_any_dataset_changes(tmp_path):
    data, files = _copy_data(tmp_path)
    before = dv.data_version(str(data), files)
    assert before == dv.data_version(str(data), files), "같은 데이터인데 지문이 달라졌다"

    path = data / files[0]
    items = json.loads(path.read_text(encoding="utf-8"))
    items[0]["content"] += " "
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    assert dv.data_version(str(data), files) != before, (
        "데이터셋 내용이 바뀌었는데 지문이 같다 — 배포 서버가 옛 저장소를 계속 쓴다 (rag/README 불변식)"
    )


def test_rebuild_only_when_version_differs():
    assert dv.needs_rebuild(None, "abc"), "지문이 없는 옛 저장소는 다시 만들어야 한다"
    assert dv.needs_rebuild({"hnsw:space": "cosine"}, "abc"), "지문이 없는 옛 저장소는 다시 만들어야 한다"
    assert dv.needs_rebuild({"data_version": "old"}, "abc")
    assert not dv.needs_rebuild({"data_version": "abc"}, "abc"), "데이터가 같은데 저장소를 지우면 자동 수집분까지 매번 사라진다"


def test_loader_uses_the_rebuild_decision():
    # 판단 함수가 있어도 로더가 부르지 않으면 아무 일도 없다.
    src = (ROOT / "rag" / "loader.py").read_text(encoding="utf-8")
    assert "needs_rebuild(" in src and "delete_collection(" in src, (
        "로더가 데이터 지문으로 저장소를 다시 만들지 않는다 — rag/README 불변식 '데이터셋이 바뀌면 저장소를 다시 만든다'"
    )


# ── chromadb 가 있는 곳에서만: 실제 저장소 왕복 ─────────────────────────────────────
# 모듈 맨 위에서 importorskip 하면 위의 판단 테스트까지 통째로 건너뛴다. 이 테스트에만 건다.
needs_chromadb = pytest.mark.skipif(
    importlib.util.find_spec("chromadb") is None,
    reason="CI 테스트 단계에는 chromadb 가 없다 — 위 판단 테스트가 대신 지킨다",
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

    import rag.loader as loader

    class FakeEmbedding(EmbeddingFunction[Documents]):
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
            return FakeEmbedding()

    data, _ = _copy_data(tmp_path)
    monkeypatch.setattr(loader, "_DATA_DIR", str(data))
    monkeypatch.setattr(loader, "_CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setattr(loader, "OpenAIEmbeddingFunction", FakeEmbedding)
    return loader, data


def _metas(collection):
    return collection.get(include=["metadatas"])["metadatas"]


@needs_chromadb
def test_store_round_trip(store):
    loader, data = store
    first = loader.init_collection("unused")
    first.add(ids=["extra"], documents=["추가"], metadatas=[{"title": "추가 항목"}])

    same = loader.init_collection("unused")
    assert "추가 항목" in {m.get("title") for m in _metas(same)}, "데이터셋이 그대로인데 저장소를 다시 만들었다"

    path = data / "rag_legal_dataset.json"
    items = json.loads(path.read_text(encoding="utf-8"))
    items[0]["content"] = "고친 내용"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")

    rebuilt = loader.init_collection("unused")
    metas = _metas(rebuilt)
    assert "추가 항목" not in {m.get("title") for m in metas}, "데이터셋이 바뀌었는데 저장소를 다시 만들지 않았다"
    assert "고친 내용" in {m.get("content") for m in metas}
