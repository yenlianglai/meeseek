"""The GitHub pack: its tools, the repository fence, and what each call asks GitHub."""

import json

import pytest

import meeseek_pack_github
from meeseek_pack_github import github


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("MEESEEK_PACK_CONFIG", json.dumps({"repos": ["octo/app", "octo/docs"]}))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@pytest.fixture
def asked(monkeypatch):
    calls, answers = [], {}

    def request(url, **kw):
        calls.append((kw.get("method", "GET"), url, kw.get("headers", {}), kw.get("body")))
        return next((a for path, a in answers.items() if path in url), {})

    monkeypatch.setattr(github.http, "request", request)
    return calls, answers


def test_one_write_and_three_reads():
    tools = {t.name: t for t in meeseek_pack_github.tools()}
    assert {n for n, t in tools.items() if t.writes} == {"github_comment"}
    assert "octo/app" in tools["github_get_issue"].parameters["properties"]["repo"]["description"]
    assert meeseek_pack_github.placeholders() == {"github_repo": "octo/app"}


def test_only_the_configured_repositories_are_reached(asked):
    with pytest.raises(ValueError, match="not one of the configured"):
        github.get_issue(1, "someone/else")
    assert github._repo("") == "octo/app"


def test_a_search_is_scoped_to_the_repository(asked):
    calls, answers = asked
    answers["search/issues"] = {"total_count": 1, "items": [
        {"number": 7, "title": "Crash on start", "state": "open", "user": {"login": "ann"},
         "labels": [{"name": "bug"}], "pull_request": {}}]}
    out = github.search_issues("crash", state="open")
    assert "repo%3Aocto%2Fapp" in calls[0][1] and "state%3Aopen" in calls[0][1]
    assert "#7 [PR, open] Crash on start — ann (bug)" in out
    assert "Authorization" not in calls[0][2], "no token: public, unauthenticated"


def test_a_token_is_sent_when_there_is_one(asked, monkeypatch):
    calls, answers = asked
    answers["issues/3"] = {"number": 3, "title": "t", "state": "open", "user": {"login": "ann"}}
    monkeypatch.setenv("GITHUB_TOKEN", "t0k")
    assert github.get_issue(3).startswith("- #3 [issue, open] t — ann")
    assert calls[0][2]["Authorization"] == "Bearer t0k"


def test_a_pull_requests_diff_is_capped(asked):
    calls, answers = asked
    answers["/files"] = [{"filename": f"f{i}.py", "status": "modified", "patch": "+x\n" * 2000} for i in range(5)]
    answers["pulls/9"] = {"number": 9, "title": "t", "state": "open", "head": {"ref": "fix"}, "base": {"ref": "main"},
                          "changed_files": 5}
    out = github.get_pull_request(9)
    assert "fix → main" in out and "### f0.py" in out and "more file(s) not shown" in out


def test_a_comment_posts_and_a_failure_says_failed(asked):
    calls, answers = asked
    answers["comments"] = {"html_url": "https://github.com/octo/app/issues/3#c1"}
    assert github.comment(3, "LGTM").startswith("Comment posted on #3")
    assert calls[-1][0] == "POST" and calls[-1][3] == {"body": "LGTM"}
    answers["comments"] = {"error": "HTTP 403: Resource not accessible"}
    assert github.comment(3, "LGTM").startswith("Failed to comment on #3")
