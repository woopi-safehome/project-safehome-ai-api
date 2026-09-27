"""지식 데이터셋의 지문과 "저장소를 다시 만들지" 판단.

chromadb 를 불러오지 않는다 — CI 의 테스트 단계는 무거운 런타임 의존성 없이 돌기 때문에,
판단 로직만 따로 두어 거기서도 검사되게 한다. 실제 저장소 조작은 loader 가 한다.
"""
import hashlib
import os


def data_version(data_dir: str, filenames: list[str]) -> str:
    """데이터셋 파일 내용의 지문. 한 글자만 바뀌어도 달라진다. 목록 순서대로 이어 붙여 계산한다."""
    h = hashlib.sha256()
    for filename in filenames:
        with open(os.path.join(data_dir, filename), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def needs_rebuild(collection_metadata: dict | None, version: str) -> bool:
    """저장소에 남긴 지문이 지금 데이터셋과 다르면(또는 없으면) 다시 만든다."""
    return (collection_metadata or {}).get("data_version") != version
