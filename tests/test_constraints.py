"""
이 저장소가 유지해야 할 성질을 실행 가능한 형태로 고정한다.

app.py 는 임포트 시 OpenAI 클라이언트를 만들고 스케줄러를 띄우므로 키 없이는
임포트되지 않는다. 그래서 그쪽은 소스를 파싱해서 본다. 판정 모듈은 부작용 없이
임포트되므로 실제로 불러서 본다 - 소스를 어떻게 썼든 나오는 결과를 본다.

어느 쪽이든 외부 호출은 일어나지 않는다. CI 는 pytest 만 설치하면 된다.
"""

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app.py"
RETRIEVER = ROOT / "rag" / "retriever.py"
DOCKERFILE = ROOT / "Dockerfile"
DATA_DIR = ROOT / "data"


# ── AST 도우미 ────────────────────────────────────────────────────────────────

def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} 함수를 찾지 못했다")


def _assigned_list(tree: ast.Module, name: str) -> list:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    raise AssertionError(f"{name} 을 찾지 못했다")


def _assigned_source(path: Path, tree: ast.Module, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.get_source_segment(source, node.value) or ""
    raise AssertionError(f"{name} 을 찾지 못했다")


def _checklist_ids() -> list:
    """판정 코드를 실제로 돌려 항목 id 를 순서대로 모은다.

    소스를 파싱하지 않는다. 판정 모듈은 부작용 없이 임포트되므로 직접 부르는 편이
    정확하다 - 코드를 어떻게 썼든 실제로 나오는 것을 본다.
    """
    from judgment import _compute_checklist
    ids = [item["id"] for item in _compute_checklist({}, {}, None)]
    assert ids, (
        '판정 코드에서 체크리스트 항목을 하나도 찾지 못하면 아무것도 보지 않고 통과한다 — README.md 의 "체크리스트 항목"'
    )
    return ids


def _analysis_calls_kwargs() -> list:
    """analyze_deed 안의 모델 호출마다 넘기는 인자를 모은다. 추출과 서술 두 호출이 있다."""
    analyze = _function(_tree(APP), "analyze_deed")
    calls = [
        {kw.arg: kw.value for kw in node.keywords if kw.arg}
        for node in ast.walk(analyze)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "create"
    ]
    assert calls, "analyze_deed 안에서 모델 호출을 찾지 못하면 아무것도 보지 않고 통과한다 — CLAUDE.md"
    return calls


# ── 결정성 ────────────────────────────────────────────────────────────────────
# 같은 입력에 같은 결과가 나와야 상류의 캐시 전략이 성립한다.
# 이 값이 흔들리면 캐시는 살아 있는데 내용이 달라지므로 아무 에러도 나지 않는다.

def test_분석_호출의_무작위성이_제거되어_있다():
    # 추출과 서술 모두 — 추출이 흔들리면 판정이, 서술이 흔들리면 같은 등기부의 설명이 매번 달라진다
    for kwargs in _analysis_calls_kwargs():
        temperature = kwargs.get("temperature")
        assert isinstance(temperature, ast.Constant), (
            "temperature 가 상수로 고정돼 있지 않다. 같은 입력에 같은 결과가 나와야 상류 캐시가 성립한다 — CLAUDE.md"
        )
        assert temperature.value == 0, (
            f"temperature 가 0 이 아니다: {temperature.value}. 같은 입력에 같은 결과가 나와야 상류 캐시가 성립한다 — CLAUDE.md"
        )

        seed = kwargs.get("seed")
        assert isinstance(seed, ast.Constant), (
            "seed 가 상수로 고정돼 있지 않다. 같은 입력에 같은 결과가 나와야 상류 캐시가 성립한다 — CLAUDE.md"
        )
        assert isinstance(seed.value, int), f"seed 가 정수가 아니다: {seed.value!r} — CLAUDE.md"


def test_모델_출력을_구조화_형식으로_강제한다():
    for kwargs in _analysis_calls_kwargs():
        response_format = kwargs.get("response_format")
        assert isinstance(response_format, ast.Dict), (
            "response_format 이 지정돼 있지 않다. 자유 텍스트를 긁어내면 파싱이 조용히 어긋난다 — CLAUDE.md"
        )
        values = [v.value for v in response_format.values if isinstance(v, ast.Constant)]
        assert "json_object" in values, f"구조화 출력이 아니다: {values} — CLAUDE.md"


# ── 체크리스트 항목의 일관성 ──────────────────────────────────────────────────
# 항목 id 는 세 곳에 흩어져 있다. 어긋나도 에러가 나지 않는다.
#   프롬프트에 없으면      → 그 항목만 서술이 비어 나간다
#   우선순위에 없으면      → 근거 부착에서 맨 뒤로 밀린다

def test_체크리스트_id_가_검색_우선순위와_일치한다():
    ids = set(_checklist_ids())
    priority = set(_assigned_list(_tree(RETRIEVER), "_RISK_PRIORITY"))
    assert ids == priority, (
        f"판정 코드에만 있는 항목={sorted(ids - priority)}, "
        f"우선순위에만 있는 항목={sorted(priority - ids)}. "
        '항목을 더할 때 고칠 곳과 순서 — README.md 의 "작업 레시피"'
    )


def test_체크리스트_id_가_시스템_프롬프트에_모두_등장한다():
    prompt = _assigned_source(APP, _tree(APP), "NARRATIVE_SYSTEM_PROMPT")
    missing = [i for i in _checklist_ids() if f'"{i}"' not in prompt]
    assert not missing, (
        f"서술 프롬프트가 서술을 요구하지 않는 항목: {missing}. "
        '항목을 더할 때 고칠 곳과 순서 — README.md 의 "작업 레시피"'
    )


def test_체크리스트_항목_수는_계약값이다():
    ids = _checklist_ids()
    assert len(ids) == 11, (
        f'항목 수가 바뀌었다: {len(ids)}개. 앱이 이 수에 맞춘다 — README.md 의 "API→AI API 계약"'
    )
    assert len(set(ids)) == len(ids), '중복된 항목 id 가 있다 — README.md 의 "체크리스트 항목"'


# ── 배포 ──────────────────────────────────────────────────────────────────────

def test_워커는_하나다():
    cmd = [l for l in DOCKERFILE.read_text(encoding="utf-8").splitlines() if l.startswith("CMD")]
    assert cmd, 'Dockerfile 에 CMD 가 없다 — rag/README.md 의 "불변식"'
    tokens = re.findall(r'"([^"]*)"', cmd[0])
    assert "-w" in tokens, '워커 수가 명시돼 있지 않다 — rag/README.md 의 "불변식"'
    assert tokens[tokens.index("-w") + 1] == "1", (
        '워커가 1이 아니다. 벡터 저장소가 프로세스마다 저장 파일을 잡아 둘 이상이면 충돌한다 — rag/README.md 의 "불변식"'
    )


# ── 지식 데이터 ───────────────────────────────────────────────────────────────
# 청크 하나에 필수 키가 빠지면 그 청크만 건너뛰는 것이 아니라 적재가 통째로
# 중단된다. 그리고 검색 실패는 치명적으로 다루지 않으므로 서비스는 지식 없이
# 그대로 뜨고, 분석 품질만 조용히 떨어진다.

_REQUIRED_KEYS = {"id", "source", "article", "title", "content", "risk_context", "tags"}


def _chunks():
    found = [
        (path.name, chunk)
        for path in sorted(DATA_DIR.glob("*.json"))
        for chunk in json.loads(path.read_text(encoding="utf-8"))
    ]
    assert found, (
        '지식 데이터를 하나도 찾지 못하면 아무것도 보지 않고 통과한다 — data/README.md 의 "청크 스키마"'
    )
    return found


def test_모든_청크가_필수_키를_갖는다():
    missing = [
        (name, chunk.get("id", "<id 없음>"), sorted(_REQUIRED_KEYS - set(chunk)))
        for name, chunk in _chunks()
        if not _REQUIRED_KEYS <= set(chunk)
    ]
    assert not missing, (
        f"필수 키가 빠진 청크: {missing}. 하나라도 빠지면 적재가 통째로 중단된다 — "
        'data/README.md 의 "청크 스키마"'
    )


def test_청크_id_는_전체에서_유일하다():
    seen, dup = set(), []
    for _, chunk in _chunks():
        cid = chunk.get("id")
        (dup.append(cid) if cid in seen else seen.add(cid))
    assert not dup, f'중복된 청크 id: {dup}. 겹치면 적재가 실패한다 — data/README.md 의 "조용히 깨지는 것들"'


# ── 이미지 구성 ───────────────────────────────────────────────────────────────
# 앱이 불러오는 로컬 모듈이 이미지에 빠지면, 테스트는 초록인데 컨테이너만 뜨지 못한다.

def _local_imports() -> set:
    names = set()
    for path in [APP, *sorted((ROOT / "rag").glob("*.py"))]:
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return {n for n in names if (ROOT / f"{n}.py").is_file() or (ROOT / n / "__init__.py").is_file()}


def test_이미지가_앱이_불러오는_로컬_모듈을_모두_담는다():
    local = _local_imports()
    assert local, (
        '앱이 불러오는 로컬 모듈을 하나도 찾지 못하면 아무것도 보지 않고 통과한다 — docker/README.md 의 "알아야 할 규칙"'
    )
    copied = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^COPY\s+(?!--from)(\S+)", line)
        if m:
            copied.add(m.group(1).rstrip("/").removesuffix(".py"))
    missing = sorted(local - copied)
    assert not missing, (
        f"이미지에 빠진 모듈: {missing}. 테스트는 통과해도 컨테이너가 뜨지 못한다 — "
        'docker/README.md 의 "알아야 할 규칙"'
    )
