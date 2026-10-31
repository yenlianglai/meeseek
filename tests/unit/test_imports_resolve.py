"""Every `from meeseek… import …` in the source resolves — the lazy ones in
function bodies too, which fail only when that code runs, and silently when
a try/except is around them (the turn record was lost that way once)."""

import ast
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = [*sorted((ROOT / "src" / "meeseek").rglob("*.py")), ROOT / "server.py"]


def _internal_imports():
    for path in FILES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("meeseek") and not node.level:
                yield path, node.lineno, node.module, [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("meeseek"):
                        yield path, node.lineno, a.name, []


def test_every_internal_import_resolves():
    broken = []
    for path, line, module, names in _internal_imports():
        where = f"{path.relative_to(ROOT)}:{line}"
        try:
            mod = importlib.import_module(module)
        except ImportError as e:
            broken.append(f"{where}: {module} ({e})")
            continue
        for name in names:
            if name != "*" and not hasattr(mod, name):
                try:
                    importlib.import_module(f"{module}.{name}")
                except ImportError:
                    broken.append(f"{where}: {module} has no {name}")
    assert broken == [], "\n".join(broken)
