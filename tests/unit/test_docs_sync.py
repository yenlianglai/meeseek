"""Docs are a copy of the code, so they drift — these tests are the join.

README trees, `.env.example`, `project.yaml.example`, the MCP tool list and
every code path a doc or docstring names are checked against what the code
reads: the code is the source of truth, never the other way round.
"""

import ast
import re

import pytest

from meeseek.config import PROJECT_ROOT

README = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
YAML_EXAMPLE = (PROJECT_ROOT / "project.yaml.example").read_text(encoding="utf-8")

_PY_FILES = sorted((PROJECT_ROOT / "src").rglob("*.py")) + [PROJECT_ROOT / "server.py"]


def _env_vars_the_code_reads() -> set[str]:
    """Every name passed to os.environ / os.getenv / secret() anywhere in the source."""
    pattern = re.compile(
        r"""(?:os\.environ(?:\.get)?|os\.getenv|\bsecret)\(\s*["']([A-Z_][A-Z0-9_]*)["']""")
    found: set[str] = set()
    for f in _PY_FILES:
        found |= set(pattern.findall(f.read_text(encoding="utf-8")))
    return found


def _documented_env_vars() -> set[str]:
    """Names in .env.example, whether set or commented out."""
    return set(re.findall(r"^#?\s*([A-Z_][A-Z0-9_]*)=", ENV_EXAMPLE, re.M))


class TestEnvExample:
    def test_every_env_var_the_code_reads_is_documented(self):
        """...except the ones project.yaml exports into the environment:
        documenting them here too would give one fact two sources."""
        from meeseek.config.loader import EXPORTED_ENV

        exported = {name for name, _ in EXPORTED_ENV}
        undocumented = sorted(_env_vars_the_code_reads() - _documented_env_vars() - exported)
        assert undocumented == [], (
            f".env.example does not mention {undocumented} — the code reads them")

    def test_exported_vars_are_not_also_documented_as_secrets(self):
        from meeseek.config.loader import EXPORTED_ENV

        both = sorted({name for name, _ in EXPORTED_ENV} & _documented_env_vars())
        assert both == [], (
            f"{both} are exported from project.yaml; setting them in .env too "
            "gives one fact two sources")

    def test_documented_vars_are_actually_read_somewhere(self):
        """A variable nobody reads is an instruction to configure nothing."""
        # Read outside the Python source: the SDKs read the first four themselves.
        external = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION_NAME",
                    "GEMINI_API_KEY", "PORT"}
        # `${VAR}` in mcp_servers[].args_from_env is read when the server starts.
        interpolated = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", YAML_EXAMPLE))
        stale = sorted(_documented_env_vars() - _env_vars_the_code_reads() - interpolated - external)
        assert stale == [], f".env.example documents {stale}, which nothing reads"


class TestProjectYamlExample:
    def test_every_top_level_config_section_is_shown(self):
        """A section missing from the example is a setting nobody discovers."""
        import dataclasses

        from meeseek.config.schema import Config

        missing = [f.name for f in dataclasses.fields(Config)
                   if not re.search(rf"^{re.escape(f.name)}:", YAML_EXAMPLE, re.M)]
        assert missing == [], f"project.yaml.example is missing section(s): {missing}"

    def test_the_example_loads_and_declares_nothing_unknown(self):
        """The loader warns on an unknown key rather than failing, so a stale
        example would otherwise sit there being quietly ignored."""
        import logging

        import yaml

        from meeseek.config.loader import build_config

        raw = yaml.safe_load(YAML_EXAMPLE) or {}
        logger = logging.getLogger("meeseek.config.loader")
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append
        logger.addHandler(handler)
        try:
            build_config(raw)
        finally:
            logger.removeHandler(handler)
        complaints = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
        assert complaints == [], f"project.yaml.example: {complaints}"


class TestReadme:
    def test_the_repo_tree_names_real_directories(self):
        """Every directory a README tree names exists."""
        for line in README.splitlines():
            m = re.match(r"^\s*(?:[│├└─\s]*)([a-z_]+)/\s{2,}", line)
            if not m:
                continue
            name = m.group(1)
            hits = list(PROJECT_ROOT.rglob(name)) + list((PROJECT_ROOT / "src").rglob(name))
            assert any(p.is_dir() for p in hits), f"README names {name}/, which does not exist"

    def test_every_installed_skill_has_a_row(self):
        from meeseek.runtime.skills import discover_skills

        for skill in discover_skills():
            assert f"| `{skill.name}` |" in README, f"README has no row for {skill.name}"

    def test_the_skills_table_lists_no_extra_skills(self):
        from meeseek.runtime.skills import discover_skills

        installed = {s.name for s in discover_skills()}
        section = README.split("## Skills", 1)[1].split("\n---", 1)[0]
        listed = set(re.findall(r"^\| `([a-z-]+)` \|", section, re.M))
        assert listed == installed, f"README skills table: {listed} != {installed}"

    def test_every_mcp_tool_is_documented(self):
        import asyncio

        from meeseek.runtime.mcp_server import build_mcp_server

        tools = {t.name for t in asyncio.run(build_mcp_server().list_tools())}
        missing = sorted(t for t in tools if f"`{t}" not in README)
        assert missing == [], f"README does not mention MCP tool(s): {missing}"


# A path into the code, as docs and docstrings write one: under a package
# directory or a repo-root directory, ending in a source or doc extension.
_CODE_PATH = re.compile(
    r"(?<![\w./-])((?:src/meeseek/|skills/|scripts/|evals/|frontend/src/)?"
    r"(?:agents|config|context|runtime|tools|prompts|skills|scripts|evals)"
    r"/[\w./-]+\.(?:py|md|sh|yaml|json|tsx?))")


def _exists(ref: str) -> bool:
    ref = ref.rstrip(".")
    if "<" in ref or "*" in ref:
        return True  # a pattern, not a path
    pkg = PROJECT_ROOT / "src" / "meeseek"
    return any((root / ref).exists() for root in (PROJECT_ROOT, pkg, pkg / "prompts"))


class TestEveryNamedPathExists:
    @pytest.mark.parametrize("name", ["README.md", "CONTRIBUTING.md", ".env.example",
                                      "project.yaml.example", "evals/README.md",
                                      "src/meeseek/prompts/README.md"])
    def test_docs(self, name):
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        missing = sorted({m.group(1) for m in _CODE_PATH.finditer(text) if not _exists(m.group(1))})
        assert missing == [], f"{name} names path(s) that do not exist: {missing}"

    def test_docstrings(self):
        offenders = []
        for f in _PY_FILES:
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    for m in _CODE_PATH.finditer(ast.get_docstring(node) or ""):
                        if not _exists(m.group(1)):
                            offenders.append(f"{f.relative_to(PROJECT_ROOT)}: {m.group(1)}")
        assert offenders == [], offenders
