"""The Jira source: what on your tickets needs you, one conversation per ticket."""

import json

import pytest

from meeseek_pack_jira import sources

ME = {"accountId": "me-1", "displayName": "Casey"}
SINCE = sources._ts("2026-10-28T08:00:00.000+0000")


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("MEESEEK_PACK_CONFIG", json.dumps({"base_url": "https://acme.atlassian.net"}))


def _issue(key, assignee="me-1", reporter="x-9", created="2026-10-01T00:00:00.000+0000"):
    return {"key": key, "fields": {"summary": f"Ticket {key}", "status": {"name": "To Do"},
                                   "assignee": {"accountId": assignee}, "reporter": {"accountId": reporter},
                                   "created": created}}


def _comment(cid, who, text, at, mention=""):
    content = [{"type": "text", "text": text}]
    if mention:
        content.append({"type": "mention", "attrs": {"id": mention, "text": "@Casey"}})
    return {"id": cid, "author": {"accountId": who, "displayName": "Casey" if who == "me-1" else "Heidi"},
            "created": at, "body": {"type": "doc", "content": [{"type": "paragraph", "content": content}]}}


def _jira(monkeypatch, issues, comments, changes=None):
    asked = []

    def get(endpoint):
        asked.append(endpoint)
        if endpoint.startswith("search/jql?"):
            return {"issues": issues}
        key = endpoint.split("/")[1]
        if "/comment?" in endpoint:
            return {"comments": comments.get(key, [])}
        return {"values": (changes or {}).get(key, [])}
    monkeypatch.setattr(sources, "_get", get)
    return asked


def test_others_comments_mentions_and_an_assignment_file_your_own_word_answers(monkeypatch):
    asked = _jira(monkeypatch, [
        _issue("ACME-1"),                                   # yours: a comment from Heidi
        _issue("ACME-2", assignee="x-9"),                   # watched: only a mention counts
        _issue("ACME-3", assignee="x-9"),                   # watched, no mention: nothing
        _issue("ACME-4"),                                   # you commented after Heidi: answered
        _issue("ACME-5", created="2026-10-28T09:00:00.000+0000"),   # new, assigned to you
        _issue("ACME-6"),                                   # you moved it: answered
    ], {
        "ACME-1": [_comment("11", "x-9", "這張還要做嗎？", "2026-10-28T09:00:00.000+0000")],
        "ACME-2": [_comment("21", "x-9", "請看 ", "2026-10-28T09:00:00.000+0000", mention="me-1")],
        "ACME-3": [_comment("31", "x-9", "FYI", "2026-10-28T09:00:00.000+0000")],
        "ACME-4": [_comment("41", "x-9", "?", "2026-10-28T09:00:00.000+0000"),
                   _comment("42", "me-1", "好", "2026-10-28T09:10:00.000+0000")],
    }, {"ACME-6": [{"created": "2026-10-28T10:00:00.000+0000", "author": {"accountId": "me-1"},
                    "items": [{"field": "status", "to": "3", "toString": "In Progress"}]}]})
    out = sources.jira_poll(since=SINCE, state={"me": ME})
    by_group = {i["group"]: i for i in out["items"]}
    assert set(by_group) == {"jira:ACME-1", "jira:ACME-2", "jira:ACME-5"}
    one = by_group["jira:ACME-1"]
    assert one["kind"] == "jira" and one["key"] == "jira:ACME-1:11" and one["body"] == "Heidi: 這張還要做嗎？"
    assert one["link"] == "https://acme.atlassian.net/browse/ACME-1" and one["title"] == "ACME-1 Ticket ACME-1"
    assert "@Casey" in by_group["jira:ACME-2"]["body"]
    assert by_group["jira:ACME-5"]["body"].startswith("這張票指派給你了")
    assert {a["group"] for a in out["answered"]} == {"jira:ACME-4", "jira:ACME-6"}
    assert "currentUser" in asked[0] and "Casey" in asked[0]


def test_a_reassignment_to_you_is_news(monkeypatch):
    _jira(monkeypatch, [_issue("ACME-7")], {}, {"ACME-7": [
        {"created": "2026-10-28T09:00:00.000+0000", "author": {"accountId": "x-9"},
         "items": [{"field": "assignee", "to": "me-1", "toString": "Casey"}]}]})
    items = sources.jira_poll(since=SINCE, state={"me": ME})["items"]
    assert [i["group"] for i in items] == ["jira:ACME-7"] and items[0]["received_at"] > SINCE


def test_your_own_account_is_asked_once(monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "_get", lambda e: calls.append(e) or (
        {"accountId": "me-1", "displayName": "Casey"} if e == "myself" else {"issues": []}))
    state = sources.jira_poll(since=SINCE)["state"]
    sources.jira_poll(since=SINCE, state=state)
    assert calls.count("myself") == 1


def test_an_api_error_fails_the_poll(monkeypatch):
    monkeypatch.setattr(sources, "_jira_get", lambda e: {"error": "HTTP 401"})
    with pytest.raises(RuntimeError, match="401"):
        sources.jira_poll(since=SINCE)
