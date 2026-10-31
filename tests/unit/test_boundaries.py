"""Who may import whom: a framework or vendor SDK is known by its adapter only,
and Google ADK by nobody.

Read from the source, not at runtime, so a lazy import inside a function
counts as much as one at the top of a file.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "meeseek"
# Rule: (an imported module prefix, the only places that may import it)
RULES = [
    ("google.adk", []),               # meeseek runs its own engine (engine.py); ADK is gone
]
# The core says what a turn is in plain Python: it imports itself, pydantic and the stdlib.
CORE = SRC / "core"
CORE_MAY_IMPORT = ("meeseek.core", "pydantic", "__future__")
# Plain code outside the package that is held to the same rules.
EXTRA = [ROOT / "server.py", *sorted((ROOT / "skills").rglob("*.py"))]


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.append((node.lineno, node.module))
    return out


def _files():
    return [*sorted(SRC.rglob("*.py")), *EXTRA]


@pytest.mark.parametrize("prefix,allowed", RULES, ids=[r[0] for r in RULES])
def test_only_its_adapter_imports_it(prefix, allowed):
    offenders = []
    for path in _files():
        rel = path.relative_to(ROOT).as_posix()
        if any(rel.startswith(a) for a in allowed):
            continue
        for line, mod in _imports(path):
            if mod == prefix or mod.startswith(prefix + "."):
                offenders.append(f"{rel}:{line} imports {mod}")
    assert offenders == [], "\n".join(offenders)


def test_the_core_knows_nothing_else():
    import sys

    offenders = []
    for path in [*sorted(CORE.rglob("*.py")), SRC / "ports.py"]:
        for line, mod in _imports(path):
            top = mod.split(".")[0]
            ok = mod.startswith(CORE_MAY_IMPORT) or top in sys.stdlib_module_names
            if not ok:
                offenders.append(f"{path.relative_to(ROOT)}:{line} imports {mod}")
    assert offenders == [], "\n".join(offenders)


def test_meeseek_never_imports_a_pack():
    """A pack is another process, reached over MCP: the core, its tests and the
    skills run, and pass, with no pack installed."""
    offenders = []
    for path in [*_files(), *sorted((ROOT / "tests").rglob("*.py"))]:
        for line, mod in _imports(path):
            if mod.startswith("meeseek_pack"):
                offenders.append(f"{path.relative_to(ROOT)}:{line} imports {mod}")
    assert offenders == [], "\n".join(offenders)


def test_a_pack_imports_no_meeseek():
    """A pack stands alone — any MCP client can run it — so it knows the kit, not meeseek."""
    offenders = []
    for path in sorted((ROOT / "packs").rglob("*.py")):
        if "/tests/" in path.as_posix():
            continue
        for line, mod in _imports(path):
            if mod == "meeseek" or mod.startswith("meeseek."):
                offenders.append(f"{path.relative_to(ROOT)}:{line} imports {mod}")
    assert offenders == [], "\n".join(offenders)
