"""review_context: the reads a review starts with, in one call."""

import time

import pytest

from meeseek_pack_gitlab import gitlab, review


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(gitlab, "_resolve_project", lambda p: p or "g/app")
    meta = {"title": "[ACME-123] fix retry", "source_branch": "ACME-123-retry",
            "head_pipeline": {"id": 9, "status": "failed"}}
    monkeypatch.setattr(gitlab, "_gitlab_get", lambda endpoint: dict(meta))

    def slow(text):
        def fn(*a, **k):
            time.sleep(0.2)
            return text
        return fn

    monkeypatch.setattr(gitlab, "gitlab_get_mr", slow("**!5: [ACME-123] fix retry**"))
    monkeypatch.setattr(gitlab, "gitlab_get_mr_diff", slow("```diff\n+retry\n```"))
    monkeypatch.setattr(gitlab, "gitlab_get_mr_comments", slow("(no comments)"))
    monkeypatch.setattr(gitlab, "gitlab_list_pipeline_jobs", slow("- test: failed"))
    return meta


def test_mr_refs_and_urls_resolve_to_project_and_iid():
    """Only allowlisted repos resolve — the URL path goes through the same gate."""
    assert gitlab.parse_mr("!165", "acme/platform/acme-app") == ("acme/platform/acme-app", 165)
    assert gitlab.parse_mr("https://gitlab.x/acme/infra/litellm/-/merge_requests/10 看一下") == (
        "acme/infra/litellm", 10)
    with pytest.raises(ValueError):
        gitlab.parse_mr("feature/x")
    with pytest.raises(ValueError, match="允許清單"):
        gitlab.parse_mr("https://gitlab.x/someone/else/-/merge_requests/1")


def test_every_part_arrives_in_one_call_read_side_by_side(world):
    started = time.monotonic()
    out = review.review_context("5")
    assert time.monotonic() - started < 0.5, "four 0.2s reads must not run one after another"
    for part in ("**!5:", "## CI\npipeline #9: failed", "- test: failed",
                 "## Ticket ACME-123\n(read its acceptance criteria", "## Discussions", "+retry"):
        assert part in out


def test_a_part_that_fails_does_not_lose_the_rest(world, monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("gitlab slow")

    monkeypatch.setattr(gitlab, "gitlab_get_mr_comments", boom)
    out = review.review_context("5")
    assert "could not read: TimeoutError: gitlab slow" in out and "+retry" in out


def test_no_ticket_and_green_ci_are_said_plainly(world):
    world.update(title="tidy", source_branch="tidy", head_pipeline={"id": 9, "status": "success"})
    out = review.review_context("5")
    assert "(no ticket key in the title or branch)" in out
    assert "pipeline #9: success" in out and "- test: failed" not in out


def test_the_mr_description_comes_back_whole(monkeypatch):
    desc = "## Why\n" + "d" * 9000 + "\nLAST LINE"
    meta = {"iid": 5, "title": "fix", "state": "opened", "author": {"name": "A"}, "source_branch": "s",
            "target_branch": "main", "created_at": "2026-10-01", "updated_at": "2026-10-02",
            "description": desc}
    monkeypatch.setattr(gitlab, "_resolve_project", lambda p: p or "g/app")
    monkeypatch.setattr(gitlab, "_gitlab_get", lambda endpoint: dict(meta))
    monkeypatch.setattr(gitlab, "gitlab_get_mr_diff", lambda *a, **k: "(diff)")
    monkeypatch.setattr(gitlab, "gitlab_get_mr_comments", lambda *a, **k: "(no comments)")
    assert desc in review.review_context("!5", project="g/app")
