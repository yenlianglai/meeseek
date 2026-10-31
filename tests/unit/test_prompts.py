"""Every prompt under prompts/ is used, every name the code asks for exists, and
every `{{placeholder}}` is filled — by project.yaml or by the caller."""

import ast
import re

import pytest

from meeseek.config import PROMPTS_DIR, home
from meeseek.config.paths import PACKAGE_ROOT
from meeseek.context import prompts
from meeseek.context.prompts import _get_placeholder_values

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def _prompt_files() -> list[str]:
    """Every prompt file, by the name `prompts.text()` takes ("agents/router")."""
    return sorted(p.relative_to(PROMPTS_DIR).with_suffix("").as_posix()
                  for p in PROMPTS_DIR.rglob("*.md") if p.name != "README.md")


def _text_calls() -> list[tuple[str, str | None, set[str]]]:
    """Every `prompts.text(...)` call in the package: (where, name, keywords).
    `name` is None when the call does not pass a string literal."""
    calls = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "text" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "prompts"):
                continue
            first = node.args[0] if node.args else None
            name = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
            where = f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}"
            calls.append((where, name, {k.arg for k in node.keywords}))
    return calls


def test_the_code_reads_prompts():
    assert len(_text_calls()) > 20


def test_every_call_names_its_prompt_literally():
    """A literal name keeps `grep 'runtime/callbacks#grace'` a complete answer."""
    dynamic = [where for where, name, _ in _text_calls() if name is None]
    assert not dynamic, f"prompts.text() without a literal name: {dynamic}"


def test_every_name_the_code_asks_for_exists():
    missing = []
    for where, name, _ in _text_calls():
        try:
            prompts.source(name)
        except (OSError, KeyError) as e:
            missing.append(f"{where}: {e}")
    assert not missing, missing


def test_every_call_supplies_exactly_the_placeholders_its_prompt_needs():
    config = set(_get_placeholder_values())
    wrong = []
    for where, name, given in _text_calls():
        needed = set(_PLACEHOLDER.findall(prompts.source(name))) - config
        if needed != given:
            wrong.append(f"{where} {name}: needs {sorted(needed)}, given {sorted(given)}")
    assert not wrong, wrong


def test_no_prompt_file_or_section_is_orphaned():
    """A file the code reads whole is used; a file read by section is used only
    if every one of its sections is."""
    used: dict[str, set[str]] = {}
    for _, name, _ in _text_calls():
        file, _, section = name.partition("#")
        used.setdefault(file, set()).add(section)
    orphans = []
    for file in _prompt_files():
        refs = used.get(file)
        if not refs:
            orphans.append(file)
        elif "" not in refs:
            defined = set(prompts.sections(prompts.source(file)))
            orphans += [f"{file}#{s}" for s in sorted(defined - refs)]
    assert not orphans, f"prompts nothing reads: {orphans}"


def test_the_manifest_lists_every_prompt_file():
    readme = (PROMPTS_DIR / "README.md").read_text(encoding="utf-8")
    unlisted = [f for f in _prompt_files() if f"`{f}.md`" not in readme]
    assert not unlisted, f"prompts/README.md is missing: {unlisted}"


def _skill_and_prompt_placeholders() -> dict[str, set[str]]:
    files = [*home().skills.glob("*/SKILL.md"),
             *(p for p in PROMPTS_DIR.rglob("*.md") if p.name != "README.md")]
    used: dict[str, set[str]] = {}
    for f in files:
        for key in _PLACEHOLDER.findall(f.read_text(encoding="utf-8")):
            used.setdefault(key, set()).add(f.name)
    return used


def test_every_engine_prompt_placeholder_is_the_cores():
    """meeseek's own prompts are rendered from project.yaml values only — never
    from a pack's, which may not be installed. (A skill may also name a pack's:
    packs/tests/test_skill_placeholders.py checks those.)"""
    defined = set(_get_placeholder_values())
    used: dict[str, set[str]] = {}
    for f in (p for p in PROMPTS_DIR.rglob("*.md") if p.name != "README.md"):
        for key in _PLACEHOLDER.findall(f.read_text(encoding="utf-8")):
            used.setdefault(key, set()).add(f.name)
    passed = {k for _, _, keywords in _text_calls() for k in keywords}    # `budget`, `tool`, … from the code
    undefined = {k: v for k, v in used.items() if k not in defined | passed}
    assert not undefined, f"undefined placeholders: {undefined}"


def test_every_defined_placeholder_is_used():
    unused = set(_get_placeholder_values()) - set(_skill_and_prompt_placeholders())
    assert not unused, f"defined but never used: {unused}"


class TestLoader:
    def test_a_whole_file_is_returned_byte_for_byte(self):
        raw = (PROMPTS_DIR / "agents" / "router.md").read_text(encoding="utf-8")
        assert prompts.source("agents/router") == raw

    def test_a_section_drops_the_blank_lines_around_it(self):
        assert prompts.sections("note\n\n## a\n\nfirst\nline\n\n## b\n\nsecond\n") == {
            "a": "first\nline", "b": "second"}

    def test_a_content_heading_is_not_a_section(self):
        assert prompts.sections("## a\n\n## 回覆風格\nx\n## Coding Conventions\ny") == {
            "a": "## 回覆風格\nx\n## Coding Conventions\ny"}

    def test_a_missing_value_raises(self):
        with pytest.raises(KeyError, match="budget"):
            prompts.text("runtime/callbacks#grace")

    def test_braces_inside_a_value_are_not_rendered(self):
        out = prompts.text("runtime/harness#reconcile", failures="{{team_name}}")
        assert "{{team_name}}" in out
