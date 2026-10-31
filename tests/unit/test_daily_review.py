"""The daily review: what it gathers, the day it files nothing, and the item it files."""

import asyncio
import json
import logging
import time

import pytest

from meeseek.app import app
from meeseek.runtime import claude_agent, daily_review, feedback, inbox, trajectory, turn_budget
from meeseek.runtime.store import JsonlStore

NOW = time.time()


@pytest.fixture
def started(monkeypatch):
    """claude_agent.start, recorded instead of run."""
    calls = []

    def fake(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return {"id": f"rec{len(calls)}"}

    monkeypatch.setattr(claude_agent, "start", fake)
    return calls


def _a_bad_day():
    JsonlStore(daily_review.log_file()).append(
        {"at": NOW - 60, "level": "WARNING", "logger": "meeseek.server", "msg": "[wakes] boom 123"})
    JsonlStore(daily_review.log_file()).append(
        {"at": NOW - 30, "level": "WARNING", "logger": "meeseek.server", "msg": "[wakes] boom 456"})
    JsonlStore(daily_review.log_file()).append(
        {"at": NOW - 3 * 86400, "level": "ERROR", "logger": "old", "msg": "last week"})
    trajectory.turns_store().append({"inv": "i1", "at": NOW - 100, "session": "s1", "skill": "sprint",
                                     "request": "update the ticket", "calls": [["jira_get_issue", True]],
                                     "auto": {"tool_errors": 1, "cap_hits": 0}, "retries": 0})
    trajectory.turns_store().append({"inv": "i2", "at": NOW - 100, "auto": {"tool_errors": 0}, "retries": 0})
    feedback.record("i1", "s1", "correction", by="detector", text="不對，我是說 PROJ-1")
    feedback.record("i2", "s2", "thumb", value=1)
    it = inbox.file(source="gmail", key="g:1", title="a mail")
    it.update(status="failed", error="model 500")
    inbox.save(it)
    app().trigger_status["chat"] = {"ok": False, "error": "403 insufficient scopes", "at": NOW}


def test_it_gathers_the_days_failures_and_only_those():
    _a_bad_day()
    b = daily_review.gather(NOW)
    assert [(g["count"], g["level"]) for g in b["log"]] == [(2, "WARNING")]       # one message, counted; last week out
    assert [t["inv"] for t in b["turns"]] == ["i1"] and b["turns"][0]["failed_tools"] == ["jira_get_issue"]
    assert [(f["kind"], f["said"]) for f in b["feedback"]] == [("correction", "不對，我是說 PROJ-1")]   # a 👍 is not
    assert [(i["status"], i["error"]) for i in b["inbox"]] == [("failed", "model 500")]
    assert b["sources"][0]["source"] == "chat"
    text = daily_review.render(b)
    assert "### turns (1)" in text and len(text) <= daily_review.BUNDLE_CHARS + 200


def test_the_bundle_stays_bounded():
    for n in range(400):
        JsonlStore(daily_review.log_file()).append(
            {"at": NOW - 10, "level": "ERROR", "logger": f"l{n}", "msg": "x" * 1500})
    b = daily_review.gather(NOW)
    assert len(b["log"]) == daily_review.SECTION_ROWS
    assert len(daily_review.render(b)) <= daily_review.BUNDLE_CHARS + 200


def test_the_log_handler_keeps_warnings_without_secrets():
    handler = daily_review._WarningLog()
    log = logging.getLogger("t.review")
    log.addHandler(handler)
    try:
        log.info("fine")
        log.warning("GET https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ/getUpdates failed")
    finally:
        log.removeHandler(handler)
    rows = JsonlStore(daily_review.log_file()).read()
    assert len(rows) == 1 and "ABCDEFGHIJ" not in rows[0]["msg"] and "[redacted]" in rows[0]["msg"]


def test_a_quiet_day_files_nothing_and_spends_no_turn(started):
    assert daily_review.run(NOW) is None
    assert not inbox.items() and not started and turn_budget.turns_today() == 0


def test_nothing_new_since_the_last_review_files_nothing(started):
    _a_bad_day()
    assert daily_review.run(NOW) is not None
    assert not daily_review.new_keys(daily_review.gather(NOW + 60))


def test_a_spent_day_budget_files_nothing(started, monkeypatch):
    _a_bad_day()
    monkeypatch.setattr(turn_budget, "cap_reached", lambda: True)
    assert daily_review.run(NOW) is None and not started


def test_a_day_with_failures_hands_claude_the_bundle_read_only(started):
    _a_bad_day()
    item = daily_review.run(NOW)
    assert item["title"].startswith("Daily review — ") and item["kind"] == "review"
    assert item["status"] == "working" and turn_budget.turns_today() == 1
    call = started[0]
    assert call["read_only"] and call["cwd"] == str(daily_review._repo())
    assert call["origin"] == {"review_item": item["id"]} and "update the ticket" in call["prompt"]
    assert inbox.get(item["id"])["meta"]["record"] == "rec1"


REPLY = """## 修正清單

1. **Retry the wake** — 原因在 runtime/wakes.py:120，每週省 20 分鐘。

```json
[{"title": "Retry a failed wake once", "fix": "In runtime/wakes.py, rearm on a 5xx.", "saves": "20 min/week"},
 {"title": "Show the chat scope error", "fix": "Explain the 403 in triggers._explain.", "saves": "5 min/week"},
 {"title": ""}]
```"""


def _reviewed(started):
    _a_bad_day()
    item = daily_review.run(NOW)
    record = {"id": "rec1", "status": "done", "origin": {"review_item": item["id"]},
              "handoffs": [{"from": "claude", "text": REPLY}]}
    asyncio.run(daily_review._on_claude_turn(record))
    return inbox.get(item["id"])


def test_the_item_lists_each_fix_as_a_held_action(started):
    item = _reviewed(started)
    assert item["status"] == "needs_approval" and not item["seen"]
    assert "```json" not in item["reply"] and item["reply"].startswith("## 修正清單")
    acts = item["actions"]
    assert [a["args"]["title"] for a in acts] == ["Retry a failed wake once", "Show the chat scope error"]
    assert all(a["tool"] == daily_review.FIX_TOOL and a["status"] == "pending" for a in acts)
    assert acts[0]["args"]["branch"].endswith("-1-retry-a-failed-wake-once")
    assert acts[0]["args"]["branch"].startswith("review/")


def test_an_approved_fix_is_a_claude_session_on_its_own_branch(started, monkeypatch, tmp_path):
    item = _reviewed(started)
    monkeypatch.setattr(daily_review, "_worktree", lambda branch, aid: tmp_path)
    first, second = item["actions"]
    inbox.approve(item["id"], first["id"])
    inbox.reject(item["id"], second["id"], "not now")
    asyncio.run(inbox.work(item["id"], "go"))
    item = inbox.get(item["id"])
    assert item["actions"][0]["status"] == "running" and item["status"] == "working"
    fix = started[-1]
    assert fix["cwd"] == str(tmp_path) and not fix.get("read_only")
    assert first["args"]["branch"] in fix["prompt"] and "Do not push" in fix["prompt"]
    done = {"id": "rec2", "status": "done", "origin": fix["origin"],
            "handoffs": [{"from": "claude", "text": "committed abc123"}]}
    asyncio.run(daily_review._on_claude_turn(done))
    item = inbox.get(item["id"])
    assert item["actions"][0]["status"] == "done" and item["actions"][0]["result"] == "committed abc123"
    assert item["status"] == "ready"


def test_a_failed_review_says_so(started):
    _a_bad_day()
    item = daily_review.run(NOW)
    record = {"id": "rec1", "status": "failed", "origin": {"review_item": item["id"]},
              "handoffs": [{"from": "claude", "text": "error_max_turns"}]}
    asyncio.run(daily_review._on_claude_turn(record))
    item = inbox.get(item["id"])
    assert item["status"] == "failed" and item["error"] == "error_max_turns" and not item["actions"]


def test_parse_fixes_ignores_a_reply_without_a_list():
    assert daily_review.parse_fixes("nothing to fix") == []
    assert daily_review.parse_fixes("```json\nnot json\n```") == []
    assert daily_review.parse_fixes('```json\n{"title": "x"}\n```') == []


def test_a_read_only_task_writes_no_file(tmp_path):
    assert claude_agent.policy(tmp_path, "Write", {"file_path": str(tmp_path / "a.py")}) == ""
    assert "read-only" in claude_agent.policy(tmp_path, "Write", {"file_path": str(tmp_path / "a.py")}, True)


def test_the_phone_asks_about_a_fix_in_words():
    from meeseek.runtime import messengers

    text = messengers.describe({"tool": daily_review.FIX_TOOL, "args": {
        "title": "Retry", "fix": "rearm", "branch": "review/2026-10-29-1-retry", "saves": ""}})
    assert "review/2026-10-29-1-retry" in text and "不 push" in text and json.dumps({}) not in text


def test_a_fixs_worktree_goes_two_weeks_after_its_last_run_unless_running(monkeypatch):
    import os

    from meeseek.runtime import claude_agent, daily_review

    monkeypatch.setattr(daily_review.subprocess, "run", lambda *a, **k: None)   # no git here
    root = daily_review.worktrees_dir()
    old, busy, recent = root / "old", root / "busy", root / "recent"
    long_ago = time.time() - 15 * 86400
    for d in (old, busy, recent):
        d.mkdir(parents=True)
        os.utime(d, (long_ago, long_ago))
    claude_agent.save({"id": "r1", "cwd": str(recent), "status": "done"})
    claude_agent._path("r2").write_text(json.dumps(
        {"id": "r2", "cwd": str(busy), "status": "running", "updated_at": long_ago}))
    monkeypatch.setattr(claude_agent, "running", lambda rid: rid == "r2")
    assert daily_review.prune_worktrees() == 1
    assert not old.exists() and busy.exists() and recent.exists()
