"""A meeting's action items become Jira work held on its one item: yours and
your team's, searched first — a comment where a ticket has the work, a new
ticket only for yours — and nothing written before you say yes."""

import asyncio
import json

import pytest

from meeseek.runtime import inbox, meeting_tickets
from meeseek.runtime.meeting_tickets import ActionItem, ActionItems, Decision, Decisions

SETTINGS = {"project": "ACME", "components": "Core", "summary_prefix": "[Core] ", "assignee": "me@acme.test"}


@pytest.fixture
def on(monkeypatch):
    from meeseek.config import cfg

    monkeypatch.setattr(cfg.triggers, "meeting_tickets", dict(SETTINGS))


def _fake(monkeypatch, actions, decide, tickets):
    """The model answers `actions`, then `decide(listing)`; jira_search answers `tickets(jql)`."""
    import meeseek.runtime.oneshot as oneshot
    import meeseek.runtime.packs as packs

    asked, searched = [], []

    async def aask(prompt, *, schema, **kw):
        asked.append(prompt)
        return ActionItems(items=actions) if schema is ActionItems else Decisions(decisions=decide(prompt))

    async def acall(name, args):
        assert name == "jira_search"
        searched.append(args["jql"])
        return tickets(args["jql"])

    monkeypatch.setattr(oneshot, "aask", aask)
    monkeypatch.setattr(packs, "acall", acall)
    return asked, searched


def _meeting():
    return inbox.file(source="meeting", key="meeting:m", title="Recording: Planning",
                      meta={"started": 0, "me": "Robin"})


def test_each_kind_of_action_item_gets_its_own_proposal(on, monkeypatch):
    actions = [
        ActionItem(owner="Robin", whose="me", what="盤點閒置服務", terms="閒置 服務"),
        ActionItem(owner="Casey", whose="team", what="更新登入流程", keys=["ACME-7"], terms="登入"),
        ActionItem(owner="Sasha", whose="team", what="寫部署文件", terms="部署 文件"),
        ActionItem(owner="Vendor", whose="other", what="提供素材"),
        ActionItem(owner="Robin", whose="me", what="評估快取", terms="快取"),
    ]
    decisions = [
        Decision(index=1, choice="create", summary="盤點並關閉閒置服務", description="## 背景與改動原因\n..."),
        Decision(index=2, choice="comment", ticket="ACME-7", comment="2026-10-20 Planning：Casey 更新登入流程"),
        Decision(index=3, choice="create", summary="撰寫部署文件", description="..."),
        Decision(index=4, choice="comment", ticket="ACME-99", comment="不在搜尋結果裡的票"),   # numbered as listed
    ]
    tickets = lambda jql: ("**1 issues found:**\n- **ACME-7**: [Core] 登入流程\n  - Type: Task"  # noqa: E731
                           if "ACME-7" in jql or "登入" in jql else "No issues found")
    asked, searched = _fake(monkeypatch, actions, lambda prompt: decisions, tickets)
    item = _meeting()
    listed = asyncio.run(meeting_tickets.propose(item, "# notes", title="Planning", date="2026-10-20", me="Robin"))

    assert [e["owner"] for e in listed] == ["Robin", "Casey", "Sasha", "Robin"]      # another team's: not listed
    assert [e["proposal"] for e in listed] == ["create", "comment", "skip", "skip"]
    held = inbox.get(item["id"])["actions"]
    assert [(a["tool"], a["status"], a["agent"]) for a in held] == [
        ("jira_create_issue", "pending", "meeting_tickets"), ("jira_add_comment", "pending", "meeting_tickets")]
    assert held[0]["args"] == {"project_key": "ACME", "summary": "[Core] 盤點並關閉閒置服務", "issue_type": "Task",
                               "description": "## 背景與改動原因\n...", "components": "Core",
                               "assignee": "me@acme.test"}
    assert held[1]["args"] == {"issue_key": "ACME-7", "body": "2026-10-20 Planning：Casey 更新登入流程"}
    assert inbox.get(item["id"])["meta"]["action_items"] == listed
    assert "key in (ACME-7)" in searched
    assert all("statusCategory != Done" in q for q in searched if q.startswith("project"))
    # the model saw what it may do: can_create false for the team's, the tickets found
    assert "can_create: false" in asked[1] and "ACME-7: [Core] 登入流程" in asked[1]
    assert "（沒找到）" in asked[1]


def test_the_same_proposal_twice_is_held_once(on, monkeypatch):
    actions = [ActionItem(owner="Robin", whose="me", what="重構設計文件"),
               ActionItem(owner="Robin", whose="me", what="重構設計文件（再提一次）")]
    same = dict(choice="create", summary="重構設計文件", description="d")
    _fake(monkeypatch, actions, lambda p: [Decision(index=1, **same), Decision(index=2, **same)], lambda jql: "")
    item = _meeting()
    asyncio.run(meeting_tickets.propose(item, "n", title="t", date="d"))
    assert len(inbox.get(item["id"])["actions"]) == 1


def test_team_items_may_get_new_tickets_when_asked(on, monkeypatch):
    from meeseek.config import cfg

    cfg.triggers.meeting_tickets["create_for_team"] = True
    _fake(monkeypatch, [ActionItem(owner="Casey", whose="team", what="寫文件")],
          lambda p: [Decision(index=1, choice="create", summary="[Core] 寫文件", description="d")], lambda jql: "")
    item = _meeting()
    asyncio.run(meeting_tickets.propose(item, "n", title="t", date="d"))
    [a] = inbox.get(item["id"])["actions"]
    assert a["args"]["summary"] == "[Core] 寫文件" and "assignee" not in a["args"]   # not yours: unassigned


def test_off_without_a_project(monkeypatch):
    import meeseek.runtime.oneshot as oneshot

    monkeypatch.setattr(oneshot, "aask", lambda *a, **k: pytest.fail("off: no model"))
    assert not meeting_tickets.enabled()
    assert asyncio.run(meeting_tickets.propose(_meeting(), "n", title="t", date="d")) == []


def test_both_search_answers_are_read():
    md = "**2 issues found:**\n\n- **ACME-1**: Fix login\n  - Type: Task\n- **ACME-2**: Docs"
    assert meeting_tickets.tickets_in(md) == [("ACME-1", "Fix login"), ("ACME-2", "Docs")]
    js = json.dumps({"issues": [{"key": "ACME-3", "summary": "Cache"}, {"key": "ACME-4", "fields": {"summary": "X"}}]})
    assert meeting_tickets.tickets_in(js) == [("ACME-3", "Cache"), ("ACME-4", "X")]
    assert meeting_tickets.tickets_in("No issues found for JQL") == []


def test_a_failed_search_leaves_fewer_candidates(on, monkeypatch):
    import meeseek.runtime.packs as packs

    async def broken(name, args):
        raise RuntimeError("Jira down")
    monkeypatch.setattr(packs, "acall", broken)
    assert asyncio.run(meeting_tickets.candidates(ActionItem(what="x", keys=["ACME-1"], terms="y"))) == []
