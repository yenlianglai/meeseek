"""Shared test fixtures.

Tests never read the machine's project.yaml or .env: the config is
project.yaml.example with the test repos, set before any test module imports
meeseek, and built fresh for every test so no test's change reaches another.
"""

import copy

import pytest
import yaml

from meeseek.config import PROJECT_ROOT, build_config, set_config

_TEST_RAW = yaml.safe_load((PROJECT_ROOT / "project.yaml.example").read_text(encoding="utf-8"))
_TEST_RAW["mcp_servers"]["gitlab"]["config"]["projects"] = [
    "acme/platform/acme-app", "acme/acme-app/iac", "acme/infra/litellm"]
set_config(build_config(copy.deepcopy(_TEST_RAW)))


@pytest.fixture(autouse=True)
def _config():
    c = build_config(copy.deepcopy(_TEST_RAW))
    set_config(c)
    yield c


@pytest.fixture(autouse=True)
def _home(tmp_path_factory):
    """Every test gets its own home, its folders made as at startup, and its
    own copy of the skills: no test reads or writes the real knowledge base,
    stores, caches or learned rules."""
    import shutil

    from meeseek.config.paths import Home, set_home

    root = tmp_path_factory.mktemp("home")
    skills = shutil.copytree(PROJECT_ROOT / "skills", root / "skills",
                             ignore=shutil.ignore_patterns("learned.md", "__pycache__"))
    h = Home(root=root, skills=skills)
    h.ensure()
    set_home(h)
    yield h
    set_home(None)


@pytest.fixture(autouse=True)
def _app(_config, _home):
    """And its own app: hooks, channels, statuses and tasks start empty."""
    from meeseek.app import build_app, uninstall

    a = build_app(_config, _home).install()
    yield a
    uninstall()


@pytest.fixture
def tmp_kb(_home):
    """The test's knowledge base, with sample files."""
    sources = _home.sources / "drive"
    sources.mkdir(parents=True)
    notes = _home.notes

    (sources / "doc1.md").write_text("# Design Doc\n\nAPI endpoint: /api/v1/npc\nField: character_id")
    (sources / "doc2.md").write_text("# Meeting Notes\n\n決議：使用 Redis 做 session 管理")
    (notes / "2026-01-01_correction.md").write_text("# Correction\n\n- character_id 已改為 npc_id")

    return _home.kb


@pytest.fixture
def mr_write(monkeypatch):
    """A write tool and a read in the registry, standing in for a git host's
    (the GitLab pack's comment_mr and get_mr): what the core does with a write
    — hold it, stub it in a replay, report its failure — needs one, and the
    core has no vendor of its own."""
    from meeseek.tools import registry

    write = registry._ToolDef(
        name="comment_mr", description="Post a comment on an MR.", fn=lambda **k: "Comment posted.",
        parameters=registry._params({"iid": {"type": "integer"}, "body": {"type": "string"}}, ["iid", "body"]),
        writes=True)
    read = registry._ToolDef(
        name="get_mr", description="Read an MR.", fn=lambda **k: "!7: fix cache",
        parameters=registry._params({"iid": {"type": "integer"}}, ["iid"]))
    monkeypatch.setitem(registry._INDEX, "comment_mr", write)
    monkeypatch.setitem(registry._INDEX, "get_mr", read)
    monkeypatch.setattr(registry, "GATED_TOOLS", (*registry.GATED_TOOLS, "comment_mr"))
    return write
