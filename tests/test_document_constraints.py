"""
문서가 가리키는 곳이 실제로 있는지 검사한다.

문서는 어긋나도 테스트가 멀쩡하다. 여기서는 그중 기계가 판정할 수 있는 것만 본다 -
링크, 인용한 절, 계약 표식, 머리글과 문서 지도, 제약 테스트가 실패 메시지에 적은 근거 문서.
문장이 코드 동작에 대해 사실인지는 보지 못한다. 그것은 CLAUDE.md 의 필수 절차가 맡는다.

검사 대상을 목록으로 적지 않고 저장소를 걸어서 찾는다. 목록은 또 하나의 사본이 되어 갈라진다.
"""

import ast
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
README = (ROOT / "README.md").resolve()


def _files(base: Path, suffix: str) -> list:
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not (d.startswith(".") or d in ("build", "node_modules"))]
        found += [(Path(dirpath) / f).resolve() for f in filenames if f.endswith(suffix)]
    return sorted(found)


DOCS = _files(ROOT, ".md")
TEST_SOURCES = _files(TESTS, ".py")
CONSTRAINT_TESTS = sorted(TESTS.glob("test_*constraints*.py"))


def _rel(p: Path) -> str:
    return p.resolve().relative_to(ROOT).as_posix()


def _in_repo(p: Path) -> bool:
    try:
        p.resolve().relative_to(ROOT)
        return True
    except ValueError:
        return False


def _lines(p: Path) -> list:
    return p.read_text(encoding="utf-8").splitlines()


def _leaves_repo(ref: str, origin: Path) -> bool:
    # 저장소 밖을 가리키는 경로는 워크스페이스에 함께 있을 때만 유효하므로 검사하지 않는다
    return not _in_repo(origin.parent / ref)


def _resolve_doc(ref: str, origin: Path = None):
    # 적힌 자리 기준, 저장소 루트 기준, 경로 끝이 일치하는 문서가 하나뿐인 경우 순으로 찾는다
    candidates = ([(origin.parent / ref).resolve()] if origin else []) + [(ROOT / ref).resolve()]
    for c in candidates:
        if c.is_file():
            return c
    hits = [d for d in DOCS if _rel(d).endswith("/" + ref)]
    return hits[0] if len(hits) == 1 else None


def _clean(s: str) -> str:
    return s.replace("**", "").replace("`", "").strip()


_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")


def _headings(lines: list) -> list:
    """코드 블록 밖의 제목들: (줄 번호, 단계, 제목)"""
    fenced, out = False, []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _HEADING.match(line)
        if m:
            out.append((i, len(m.group(1)), _clean(m.group(2))))
    return out


def _section_end(lines: list, heads: list, start: tuple) -> int:
    return next((i for i, level, _ in heads if i > start[0] and level <= start[1]), len(lines))


def _cites(doc: Path, section: str) -> bool:
    """"절 - 항목" 형태면 앞은 제목에, 뒤는 본문 어딘가에 있어야 한다."""
    parts = _clean(section).split(" - ")
    lines = _lines(doc)
    return any(parts[0] in title for _, _, title in _headings(lines)) and all(
        any(part in line for line in lines) for part in parts[1:]
    )


_LINK = re.compile(r"\]\(([^)\s]+)\)")
_SECTION = r'(?:"([^"]+)"|\*\*([^*]+)\*\*|([^\s"*]+(?: [^\s"*]+)?) 절(?!차))'
_DOC_CITATION = re.compile(r"([A-Za-z0-9_./-]+\.md)[`)]? ?의 " + _SECTION)
_README_CITATION = re.compile(r"README ?의 " + _SECTION)
_LOCAL_CITATION = re.compile(r'(?:위|아래) (?:"([^"]+)"|\*\*([^*]+)\*\*)')


def _cited(m, start: int) -> str:
    return next(g for g in m.groups()[start:] if g)


def _mapped_docs():
    """문서 지도 절이 가리키는 문서들. 절이 없으면 None."""
    lines = _lines(README)
    heads = _headings(lines)
    start = next((h for h in heads if "문서 지도" in h[2]), None)
    if start is None:
        return None
    targets = []
    for line in lines[start[0]:_section_end(lines, heads, start)]:
        for m in _LINK.finditer(line):
            p = (ROOT / m.group(1).split("#")[0]).resolve()
            if p.name != "CLAUDE.md":
                targets.append(p)
    return targets


def test_문서의_링크가_가리키는_파일이_있다():
    broken = []
    for doc in DOCS:
        for m in _LINK.finditer(doc.read_text(encoding="utf-8")):
            target = m.group(1).split("#")[0]
            if not target or target.startswith(("http:", "https:", "mailto:")) or _leaves_repo(target, doc):
                continue
            if not (doc.parent / target).exists():
                broken.append(f"{_rel(doc)} → {m.group(1)}")
    assert not broken, f"깨진 링크: {broken}. 경로를 옮기면 그곳을 가리키는 문서도 함께 고친다 — CLAUDE.md"


def test_문서와_테스트에_적힌_문서_경로가_있다():
    missing = []
    for doc in DOCS:
        for ref in re.findall(r"`([A-Za-z0-9_./-]+\.md)`", doc.read_text(encoding="utf-8")):
            if not _leaves_repo(ref, doc) and _resolve_doc(ref, doc) is None:
                missing.append(f"{_rel(doc)} → {ref}")
    for src in TEST_SOURCES:
        for ref in re.findall(r"[A-Za-z0-9_./-]+\.md", src.read_text(encoding="utf-8")):
            if not ref.startswith("../") and _resolve_doc(ref) is None:
                missing.append(f"{_rel(src)} → {ref}")
    assert not missing, f"없는 문서: {missing}. 문서를 옮기거나 지우면 이름을 적은 곳도 함께 고친다 — CLAUDE.md"


def test_인용한_절이_그_문서에_있다():
    problems = []

    def verify(at, ref, target, section):
        if target is None:
            problems.append(f"{at} → {ref} 가 없다")
        elif not _cites(target, section):
            problems.append(f'{at} → {ref} 에 "{section}" 절이 없다')

    for doc in DOCS:
        for i, line in enumerate(_lines(doc), 1):
            at = f"{_rel(doc)}:{i}"
            for m in _DOC_CITATION.finditer(line):
                if not _leaves_repo(m.group(1), doc):
                    verify(at, m.group(1), _resolve_doc(m.group(1), doc), _cited(m, 1))
            for m in _README_CITATION.finditer(line):
                verify(at, "README.md", README, _cited(m, 0))
            for m in _LOCAL_CITATION.finditer(line):
                verify(at, _rel(doc), doc, _cited(m, 0))
    for src in TEST_SOURCES:
        for i, line in enumerate(_lines(src), 1):
            for m in _DOC_CITATION.finditer(line):
                verify(f"{_rel(src)}:{i}", m.group(1), _resolve_doc(m.group(1)), _cited(m, 1))

    assert not problems, f"어긋난 인용: {problems}. 절 이름을 바꾸면 인용한 곳도 함께 고친다 — CLAUDE.md"


def test_계약_절은_스스로_계약임을_밝힌다():
    unmarked = []
    for doc in DOCS:
        lines = _lines(doc)
        heads = _headings(lines)
        for h in heads:
            if "계약" in h[2] and not any("이 절은 계약이다" in l for l in lines[h[0]:_section_end(lines, heads, h)]):
                unmarked.append(f"{_rel(doc)} → {h[2]}")
    assert not unmarked, (
        f"표식 없는 계약 절: {unmarked}. 표식이 없으면 코드를 뒤따르는 설명으로 읽혀, "
        f"어긋났을 때 코드가 아니라 문서를 고치게 된다 — CLAUDE.md"
    )


def test_스스로_문서임을_밝힌_문서는_문서_지도에_있다():
    mapped = _mapped_docs()
    assert mapped is not None, "README.md 에 문서 지도 절이 없다 — CLAUDE.md"
    declared = [d for d in DOCS if d != README and any(l.startswith("> **범위**") for l in _lines(d)[:15])]
    unmapped = [_rel(d) for d in declared if d not in mapped]
    assert not unmapped, f"문서 지도에 없는 문서: {unmapped}. 찾아갈 길이 없는 문서는 읽히지 않는다 — CLAUDE.md"


def test_머리글이_범위와_책임을_밝힌다():
    problems = []
    for doc in [README] + (_mapped_docs() or []):
        if not doc.is_file():
            continue
        head = " ".join(l for l in _lines(doc)[:15] if l.startswith(">"))
        labels = ("범위", "여기 없는 것") + (() if doc == README else ("상위",))
        problems += [f"{_rel(doc)} → {label}" for label in labels if f"**{label}**" not in head]
    assert not problems, f"머리글에 빠진 것: {problems}. 어디까지 믿고 어디부터 코드를 볼지 알려준다 — CLAUDE.md"


def test_제약_테스트는_실패_메시지에_근거_문서를_담는다():
    problems = []
    for path in CONSTRAINT_TESTS:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assert) and (node.msg is None or ".md" not in ast.unparse(node.msg)):
                problems.append(f"{_rel(path)}:{node.lineno}")
    assert not problems, (
        f"근거 문서가 없는 검사: {problems}. 어긴 순간에 문서가 도착해야 한다. "
        f"주석이 아니라 실패 메시지에 적는다 — CLAUDE.md"
    )
