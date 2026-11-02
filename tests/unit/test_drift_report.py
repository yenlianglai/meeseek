"""The drift report: what it compares, the Claude run it starts, and the one
Inbox item (or none) it files."""

import json
import os
import time

import pytest

from meeseek.runtime import claude_agent, drift_report, inbox, turn_budget
from meeseek.runtime import workstreams as ws_mod

NOW = time.time()


@pytest.fixture
def started(monkeypatch):
    calls = []

    def fake(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return {"id": f"rec{len(calls)}"}

    monkeypatch.setattr(claude_agent, "start", fake)
    return calls


@pytest.fixture
def kb(_home):
    drive = _home.sources / "drive"
    drive.mkdir(parents=True, exist_ok=True)
    (drive / "[Team] engineering-plan.md").write_text(
        "> 文件最後更新：2026-10-30\n# Engineering plan\n## Milestones\n## API index\n", encoding="utf-8")
    (drive / "api-spec.md").write_text("# SDK\n## Events\n", encoding="utf-8")
    (drive / "Weekly meeting - 2026_10_13 - notes.md").write_text("# Weekly meeting\n", encoding="utf-8")
    return drive


def _reply(findings) -> dict:
    text = "檢查了 2 份文件。\n\n```json\n" + json.dumps(findings, ensure_ascii=False) + "\n```"
    return {"id": "rec1", "status": "done", "origin": {"drift": "2026-11-02"},
            "handoffs": [{"from": "claude", "text": text}]}


FINDING = {"topic": "Chat timeout", "document": "sources/drive/api-spec.md",
           "document_says": "Events: chat times out after 30 s", "code_says": "CHAT_TIMEOUT_S = 60",
           "where": "app/chat.py:12; api-spec § Events", "refs": ["PROJ-12"]}


def test_it_compares_the_design_documents_not_the_meeting_notes(kb, monkeypatch):
    monkeypatch.setattr(ws_mod, "_ticket_projects", lambda: {"PROJ"})
    ws_mod.resolve(["PROJ-12"], title="Chat timeout")
    b = drift_report.gather()
    paths = {d["path"] for d in b["docs"]}
    assert paths == {"sources/drive/[Team] engineering-plan.md", "sources/drive/api-spec.md"}
    eng = next(d for d in b["docs"] if "engineering-plan" in d["path"])
    assert eng["updated"] == "2026-10-30" and eng["headings"] == ["Engineering plan", "Milestones", "API index"]
    assert b["repo"] == "acme/platform/acme-app" and b["workstreams"][0]["key"] == "PROJ-12"
    text = drift_report.render(b)
    assert "Main repo: acme/platform/acme-app" in text and len(text) <= drift_report.BUNDLE_CHARS + 20


def test_the_configured_documents_narrow_it_and_it_stays_bounded(kb, _config):
    _config.triggers.drift_docs = ["engineering-plan"]
    assert [d["path"] for d in drift_report.gather()["docs"]] == ["sources/drive/[Team] engineering-plan.md"]
    _config.triggers.drift_docs = []
    later = time.time() + 3600            # newer than the fixture's documents, however long the run
    for n in range(30):
        p = kb / f"design-{n}.md"
        p.write_text("# d\n", encoding="utf-8")
        os.utime(p, (later + n, later + n))
    docs = drift_report.gather()["docs"]
    assert len(docs) == drift_report.MAX_DOCS and docs[0]["path"].endswith("design-29.md")   # the newest first


def test_a_run_is_one_read_only_claude_turn_in_the_knowledge_base(kb, started, _home):
    record = drift_report.run(NOW)
    [call] = started
    assert record["id"] == "rec1" and call["read_only"] is True and call["cwd"] == str(_home.kb)
    assert "api-spec.md" in call["prompt"] and "get_file" in call["prompt"]
    assert turn_budget.turns_today() == 1
    assert drift_report._on_claude_turn in drift_report.app().after_claude_turn


def test_no_run_without_documents_or_budget(started, _home, kb, _config):
    _config.triggers.daily_turns = 1
    turn_budget.count_turn()
    assert drift_report.run(NOW) is None and started == []
    _config.triggers.daily_turns = 0
    for p in kb.iterdir():
        p.unlink()
    assert drift_report.run(NOW) is None and started == []


def test_each_inconsistency_goes_into_one_item_and_onto_its_workstream(monkeypatch):
    monkeypatch.setattr(ws_mod, "_ticket_projects", lambda: {"PROJ"})
    ws = ws_mod.resolve(["PROJ-12"])
    other = {**FINDING, "topic": "Milestone date", "refs": [], "document_says": "Beta on 11/15",
             "code_says": "docs/release.md: beta on 12/01", "where": "docs/release.md:4"}
    item = drift_report.finish(_reply([FINDING, other]))
    assert item["title"] == "Drift report — 2026-11-02" and item["status"] == "ready"
    assert "CHAT_TIMEOUT_S = 60" in item["body"] and "Beta on 11/15" in item["body"]
    assert item["meta"]["found"] == 2 and item["meta"]["noted"] == 1
    [event] = ws_mod.get(ws["id"])["events"]
    assert event["by"] == "drift report" and "Chat timeout" in event["text"]
    assert inbox.by_key("drift:2026-11-02")["id"] == item["id"]
    assert drift_report.finish(_reply([FINDING])) is None                  # one item a run


def test_no_inconsistency_files_nothing_and_an_unfinished_one_says_so():
    assert drift_report.finish(_reply([])) is None and inbox.items(10) == []
    failed = {"id": "rec2", "status": "failed", "origin": {"drift": "2026-11-09"},
              "handoffs": [{"from": "claude", "text": "", "error": "max turns"}]}
    item = drift_report.finish(failed)
    assert item["status"] == "failed" and item["error"].startswith("The comparison did not finish")
    no_list = {**_reply([]), "origin": {"drift": "2026-11-16"}, "handoffs": [{"from": "claude", "text": "I ran out."}]}
    assert drift_report.finish(no_list)["status"] == "failed"
