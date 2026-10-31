"""The GitLab pack as a client sees it: every tool reaches every allowed repo,
the MR writes say they write, and the allowlist holds."""

import inspect

import pytest

import meeseek_pack_gitlab
from gitlab_test_settings import PROJECTS
from meeseek_pack_gitlab.gitlab import _resolve_project

WRITES = {"comment_mr", "reply_mr_comment", "update_mr", "create_mr", "comment_mr_lines"}


def _tools():
    return {t.name: t for t in meeseek_pack_gitlab.tools()}


def test_the_mr_writes_are_writes_and_nothing_else_is():
    assert {n for n, t in _tools().items() if t.writes} == WRITES


def test_every_tool_accepts_project_and_forwards_it():
    """A tool whose schema or wrapper drops `project` silently answers about the
    default repo instead. (A source is not an agent's tool: it reads all your MRs.)"""
    for name, t in _tools().items():
        if t.source:
            continue
        assert "project" in t.parameters["properties"], name
        assert "project" in inspect.signature(t.fn).parameters, name


def test_every_tool_is_described_with_a_schema():
    for name, t in _tools().items():
        assert t.description.strip() and t.parameters.get("type") == "object", name
        assert t.parameters.get("properties") or name in ("list_mrs", "list_pipelines"), name


def test_listed_repo_resolves_and_the_first_is_the_default():
    assert _resolve_project("") == PROJECTS[0]
    assert _resolve_project(PROJECTS[1]) == PROJECTS[1]


def test_unlisted_repo_rejected_with_allowed_list():
    """The token reaches every repo its owner can — the allowlist is the fence."""
    with pytest.raises(ValueError, match="允許清單"):
        _resolve_project("some/other-repo")


def test_placeholders_name_the_primary_repo_and_the_clone(monkeypatch):
    from gitlab_test_settings import use

    use(monkeypatch, local_clone="/src/acme-app")
    assert meeseek_pack_gitlab.placeholders() == {"gitlab_project_path": PROJECTS[0], "local_clone": "/src/acme-app"}
