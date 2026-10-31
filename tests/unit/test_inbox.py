"""The Inbox: items filed by triggers, and the gate that holds every write in
an Inbox session until it is approved."""

import asyncio
import time

import pytest

from meeseek.core.events import ToolCall
from meeseek.core.tools import Effect, ToolSpec
from meeseek.core.turn import Hold, Run, TurnState
from meeseek.engine import InboxGate
from meeseek.runtime import inbox


def _spec(tool):
    return ToolSpec(name=tool, description="d", fn=lambda **k: "", parameters={"type": "object", "properties": {}},
                    effect=Effect.WRITE if tool == "create_issue" else Effect.READ)


def _state(session_id: str, user: str = inbox.INBOX_USER):
    return TurnState(session=session_id, turn="t1", watched=False, started=0.0, history=[], agent="sprint_agent",
                     by=user)


def _item(**kw):
    item = inbox.file(source="chat", key=kw.pop("key", "chat:1"), title="t", body="b")
    item["session_id"] = f"inbox-{item['id']}"
    inbox.save(item)
    return item


ARGS = {"project": "ACME", "summary": "修正排序"}


class TestFiling:
    def test_a_key_is_filed_once(self):
        assert inbox.file(source="gmail", key="gmail:1", title="t") is not None
        assert inbox.file(source="gmail", key="gmail:1", title="t") is None

    def test_a_restart_leaves_no_item_working(self):
        item = _item()
        item["status"] = "working"
        inbox.save(item)
        assert inbox.recover() == 1
        assert inbox.get(item["id"])["status"] == "failed"


class TestGate:
    """engine.InboxGate: in an Inbox turn every write waits for the person."""

    def _call(self, item, tool="create_issue", args=ARGS, user=inbox.INBOX_USER, state=None):
        t = state or _state(item["session_id"], user)
        return asyncio.run(InboxGate().on_tool(t, ToolCall(id="c", name=tool, args=dict(args)), _spec(tool)))

    def test_a_write_is_held_as_a_pending_action(self):
        item = _item()
        out = self._call(item)
        assert isinstance(out, Hold) and out.queued and "沒有執行" in out.reason
        [action] = inbox.get(item["id"])["actions"]
        assert (action["tool"], action["args"], action["status"]) == ("create_issue", ARGS, "pending")

    def test_the_same_call_twice_is_one_action(self):
        item = _item()
        self._call(item)
        self._call(item)
        assert len(inbox.get(item["id"])["actions"]) == 1

    def test_reads_and_other_users_pass(self):
        item = _item()
        assert self._call(item, tool="kb_grep") is None
        assert self._call(item, user="webui") is None
        assert inbox.get(item["id"])["actions"] == []

    def test_the_approved_call_goes_through_once(self):
        item = _item()
        self._call(item)
        [action] = inbox.get(item["id"])["actions"]
        message = inbox.approve(item["id"], action["id"])
        assert "create_issue" in message and "修正排序" in message
        assert isinstance(self._call(item), Run)              # exactly what was approved: runs
        assert inbox.get(item["id"])["actions"][0]["status"] == "running"
        assert isinstance(self._call(item), Hold)             # a second time: held again

    def test_edited_arguments_are_what_runs(self):
        item = _item()
        self._call(item)
        [action] = inbox.get(item["id"])["actions"]
        edited = {**ARGS, "summary": "修正 reranker 排序"}
        inbox.approve(item["id"], action["id"], edited)
        assert isinstance(self._call(item, args=ARGS), Hold)  # the original args are not approved
        assert isinstance(self._call(item, args=edited), Run)

    def test_the_result_is_recorded(self):
        item = _item()
        self._call(item)
        inbox.approve(item["id"], inbox.get(item["id"])["actions"][0]["id"])
        t = _state(item["session_id"])
        self._call(item, state=t)
        InboxGate().on_result(t, ToolCall(id="c", name="create_issue", args=ARGS), _spec("create_issue"),
                              {"result": "Created ACME-42"})
        action = inbox.get(item["id"])["actions"][0]
        assert (action["status"], action["result"]) == ("done", "Created ACME-42")

    def test_reject_settles_the_action(self):
        item = _item()
        self._call(item)
        action = inbox.get(item["id"])["actions"][0]
        assert "不同意" in inbox.reject(item["id"], action["id"], "先不要開票")
        assert inbox.get(item["id"])["actions"][0]["status"] == "rejected"
        with pytest.raises(ValueError):
            inbox.approve(item["id"], action["id"])


class TestWork:
    def test_the_turn_outcome_lands_on_the_item(self, monkeypatch):
        item = _item()

        async def turn(message, session_id, *, user_id):
            assert user_id == inbox.INBOX_USER and session_id == item["session_id"]
            inbox.hold(inbox.get(item["id"]), "create_issue", ARGS, "sprint_agent")
            return "對方要修排序。\n```reply\n好，我今天看\n```", "sprint_agent", []

        monkeypatch.setattr("meeseek.runtime.mcp_server.run_orchestrator_turn", turn)
        asyncio.run(inbox.work(item["id"], "go"))
        done = inbox.get(item["id"])
        assert done["status"] == "needs_approval" and done["draft"] == "好，我今天看"
        assert done["message"] == "go"

    def test_a_failed_turn_is_a_state(self, monkeypatch):
        item = _item()

        async def boom(*a, **k):
            raise RuntimeError("model down")

        monkeypatch.setattr("meeseek.runtime.mcp_server.run_orchestrator_turn", boom)
        asyncio.run(inbox.work(item["id"], "go"))
        assert inbox.get(item["id"])["status"] == "failed"


def test_a_meetings_brief_or_notes_are_done_once_opened():
    from meeseek.runtime import inbox

    item = inbox.file(source="meeting", key="meeting:1", title="Recording: Weekly")
    item["status"] = "ready"
    inbox.save(item)
    inbox.mark_seen(item["id"])
    assert inbox.get(item["id"])["status"] == "done"
    mail = inbox.file(source="gmail", key="gmail:1", title="Q", meta={"thread_id": "t"})
    mail["status"] = "ready"
    inbox.save(mail)
    inbox.mark_seen(mail["id"])
    assert inbox.get(mail["id"])["status"] == "ready"      # a mail may still want an answer


def test_your_reply_settles_only_what_came_before_it():
    from meeseek.runtime import inbox

    old = inbox.file(source="chat", key="chat:1", title="a", received_at=100.0, meta={"space": "spaces/A"})
    held = inbox.file(source="chat", key="chat:2", title="b", received_at=100.0, meta={"space": "spaces/A"})
    later = inbox.file(source="chat", key="chat:3", title="c", received_at=500.0, meta={"space": "spaces/A"})
    for i, status in ((old, "ready"), (held, "needs_approval"), (later, "ready")):
        i["status"] = status
        inbox.save(i)
    assert inbox.answered("spaces/A", 300.0) == 1
    assert [inbox.get(i["id"])["status"] for i in (old, held, later)] == ["done", "needs_approval", "ready"]


class TestStale:
    """What nobody will act on any more lets go: a brief once its meeting
    ended; a held write named once by the digest, a day later."""

    def _brief(self, end, status="ready"):
        it = inbox.file(source="calendar", kind="meeting", key=f"calendar:e:{end}", title="Daily",
                        meta={"start": "2026-10-29T10:00:00+08:00", "end": end})
        it["status"] = status
        inbox.save(it)
        return it

    def _held(self, days, **meta):
        it = inbox.file(source="chat", key=f"chat:{days}:{meta}", title="留言")
        it["meta"].update(meta)
        inbox.save(it)
        inbox.hold(it, "comment_mr", {"mr_iid": 1, "body": "x"}, "router")
        it = inbox.get(it["id"])
        it["status"] = "needs_approval"
        it["actions"][0]["at"] = time.time() - days * 86400
        inbox.save(it)
        return it

    def test_a_brief_is_done_once_its_meeting_ended(self):
        over, coming = self._brief("2026-10-29T10:30:00+08:00"), self._brief("2999-01-01T10:30:00+08:00")
        recording = inbox.file(source="meeting", key="meeting:r", title="Recording: Daily",
                               meta={"started": time.time() - 7200, "end": time.time() - 3600})
        recording["status"] = "ready"
        inbox.save(recording)
        assert inbox.expire() == 1
        assert inbox.get(over["id"])["status"] == "done"
        assert inbox.get(over["id"])["triage"]["settled_by"] == "the meeting ended"
        assert inbox.get(coming["id"])["status"] == "ready"
        assert inbox.get(recording["id"])["status"] == "ready"   # the notes are not a brief

    def test_a_stale_write_is_named_once_then_expires(self):
        fresh, stale = self._held(1), self._held(4)
        assert [i["id"] for i in inbox.stale_approvals()] == [stale["id"]]
        assert inbox.expire() == 0                               # not yet named by a digest
        inbox.note_stale([stale["id"]], time.time() - 2 * 86400)
        assert inbox.stale_approvals() == []                     # named once only
        assert inbox.expire() == 1
        after = inbox.get(stale["id"])
        assert after["status"] == "expired" and after["actions"][0]["status"] == "expired"
        assert "expired" in inbox.SETTLED and inbox.get(fresh["id"])["status"] == "needs_approval"

    def test_with_no_digest_a_stale_write_expires_unnamed(self):
        stale = self._held(4)
        assert inbox.expire(digest=False) == 1 and inbox.get(stale["id"])["status"] == "expired"
