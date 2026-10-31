"""Wakes: records on disk that survive a restart, claimed and run by the poll."""

import asyncio
import time

from meeseek.runtime import wakes


def _file(**kw):
    kw.setdefault("kind", "inbox_retry")
    kw.setdefault("due_at", time.time() - 1)
    return wakes.file(**kw)


class TestStore:
    def test_filing_the_same_key_twice_is_one_wake(self):
        a = _file(key="k1")
        b = _file(key="k1")
        assert a["id"] == b["id"]
        assert len(wakes.pending()) == 1

    def test_a_settled_key_can_be_filed_again(self):
        a = _file(key="k1")
        wakes.settle(a["id"], "done")
        b = _file(key="k1")
        assert a["id"] != b["id"]

    def test_pending_filters_by_session(self):
        _file(session_id="s1")
        _file(session_id="s2")
        assert len(wakes.pending("s1")) == 1
        assert len(wakes.pending()) == 2

    def test_an_event_sets_its_wake_due_now(self):
        w = _file(due_at=time.time() + 3600, waiting_on="claude:abc")
        assert wakes.on_event("claude:abc") == 1
        assert wakes.on_event("claude:other") == 0
        [claimed] = wakes.claim_due()
        assert claimed["id"] == w["id"]

    def test_claim_due_claims_each_wake_once(self):
        _file()
        _file(due_at=time.time() + 3600)   # not due: left alone
        assert len(wakes.claim_due()) == 1
        assert wakes.claim_due() == []

    def test_old_settled_wakes_leave_the_store_on_a_claim(self):
        w = _file()
        wakes.settle(w["id"], "done")
        wakes.claim_due(now=time.time() + (wakes.KEEP_DAYS + 1) * 86400)
        assert wakes.wake_store().read() == []

    def test_exists_counts_settled_wakes_until_pruned(self):
        w = _file(key="claude:r1")
        wakes.settle(w["id"], "done")
        assert wakes.exists("claude:r1")
        assert not wakes.has_pending("claude:r1")


class TestRecover:
    def test_an_interrupted_wake_goes_back_to_pending_due_now(self):
        _file(due_at=time.time() - 1)
        wakes.claim_due()
        assert wakes.recover() == 1
        assert len(wakes.claim_due()) == 1

    def test_a_wake_interrupted_repeatedly_fails(self):
        w = _file()
        for _ in range(wakes.MAX_ATTEMPTS):
            wakes.claim_due()
            wakes.recover()
        [r] = wakes.wake_store().read()
        assert r["id"] == w["id"] and r["status"] == "failed"


class TestPoll:
    def test_an_unknown_kind_settles_failed(self):
        w = _file(kind="nope")
        assert asyncio.run(wakes.poll()) == 1
        [r] = wakes.wake_store().read()
        assert (r["id"], r["status"]) == (w["id"], "failed") and "no handler" in r["note"]

    def test_a_handler_that_raises_settles_failed(self, monkeypatch):
        async def boom(wake):
            raise RuntimeError("x")

        monkeypatch.setitem(wakes._HANDLERS, "inbox_retry", boom)
        _file()
        asyncio.run(wakes.poll())
        [r] = wakes.wake_store().read()
        assert r["status"] == "failed" and "RuntimeError" in r["note"]

    def test_inbox_retry_starts_the_items_turn(self, monkeypatch):
        from meeseek.runtime import inbox

        started = []
        monkeypatch.setattr(inbox, "get", lambda item_id: {"id": item_id})
        monkeypatch.setattr(inbox, "start", lambda item_id, message="": started.append(item_id))
        _file(payload={"item_id": "ab12"})
        asyncio.run(wakes.poll())
        assert started == ["ab12"]
        [r] = wakes.wake_store().read()
        assert r["status"] == "done"

    def test_inbox_retry_on_a_gone_item_fails(self, monkeypatch):
        from meeseek.runtime import inbox

        monkeypatch.setattr(inbox, "get", lambda item_id: None)
        _file(payload={"item_id": "gone"})
        asyncio.run(wakes.poll())
        [r] = wakes.wake_store().read()
        assert r["status"] == "failed"


class TestClaudeFollowup:
    def _record(self, monkeypatch, status="done", reply="改好了"):
        from meeseek.runtime import claude_agent

        record = {"id": "r1", "title": "修 bug", "session_id": "sid-1", "status": status,
                  "handoffs": [{"from": "claude", "text": reply}]}
        monkeypatch.setattr(claude_agent, "get", lambda rid: record if rid == "r1" else None)
        monkeypatch.setattr(claude_agent, "running", lambda rid: status == "running")
        return record

    def test_a_finished_task_gets_a_follow_up_turn_on_the_asking_session(self, monkeypatch):
        from meeseek.runtime import inbox, mcp_server

        self._record(monkeypatch)
        monkeypatch.setattr(inbox, "by_session", lambda sid: None)
        turns = []

        async def run(request, session_id, *, user_id):
            turns.append((request, session_id, user_id))
            return "", "", []

        monkeypatch.setattr(mcp_server, "run_orchestrator_turn", run)
        _file(kind="claude_followup", payload={"record_id": "r1", "user": "u1"}, session_id="chat-1")
        asyncio.run(wakes.poll())
        [(request, session_id, user_id)] = turns
        assert "改好了" in request and "修 bug" in request and "完成了" in request
        assert (session_id, user_id) == ("chat-1", "u1")
        [r] = wakes.wake_store().read()
        assert r["status"] == "done"

    def test_an_inbox_session_goes_through_the_inbox(self, monkeypatch):
        from meeseek.runtime import inbox

        self._record(monkeypatch, status="failed", reply="卡住了")
        monkeypatch.setattr(inbox, "by_session", lambda sid: {"id": "it1"} if sid == "inbox-it1" else None)
        started = []
        monkeypatch.setattr(inbox, "start", lambda item_id, message="": started.append((item_id, message)))
        _file(kind="claude_followup", payload={"record_id": "r1"}, session_id="inbox-it1")
        asyncio.run(wakes.poll())
        [(item_id, message)] = started
        assert item_id == "it1" and "失敗了" in message

    def test_the_follow_up_is_exempt_from_the_daily_cap(self, monkeypatch):
        from meeseek.config import cfg
        from meeseek.runtime import inbox, mcp_server, turn_budget

        monkeypatch.setattr(cfg.triggers, "daily_turns", 1)
        turn_budget.count_turn()   # the day's budget is already spent
        self._record(monkeypatch)
        monkeypatch.setattr(inbox, "by_session", lambda sid: None)
        ran = []

        async def run(request, session_id, *, user_id):
            ran.append(session_id)
            return "", "", []

        monkeypatch.setattr(mcp_server, "run_orchestrator_turn", run)
        _file(kind="claude_followup", payload={"record_id": "r1"}, session_id="chat-1")
        asyncio.run(wakes.poll())
        assert ran == ["chat-1"]   # the result still reaches the asking session today

    def test_a_task_still_running_at_the_timeout_rearms(self, monkeypatch):
        self._record(monkeypatch, status="running")
        _file(kind="claude_followup", payload={"record_id": "r1"}, session_id="chat-1")
        asyncio.run(wakes.poll())
        [r] = wakes.wake_store().read()
        assert r["status"] == "pending" and r["rearmed"] == 1
        assert r["due_at"] > time.time() + wakes.FOLLOWUP_TIMEOUT_S - 60


class TestRemind:
    class Ch:
        name = "telegram"

        def __init__(self):
            self.sent = []

        async def send(self, chat, texts):
            self.sent.append((chat, texts))

    def _channel(self, monkeypatch):
        from meeseek.runtime import messengers

        ch = self.Ch()
        monkeypatch.setitem(messengers.channels(), "telegram", ch)
        return ch

    def test_a_due_reminder_is_pushed_to_the_asking_chat(self, monkeypatch):
        from meeseek.runtime import inbox, turn_budget

        ch = self._channel(monkeypatch)
        monkeypatch.setattr(inbox, "by_session",
                            lambda sid: {"source": "telegram", "meta": {"chat": "c9"}} if sid == "s1" else None)
        _file(kind="remind", session_id="s1", payload={"text": "回家吃藥", "user": "inbox"})
        asyncio.run(wakes.poll())
        assert ch.sent == [("c9", ["⏰ 回家吃藥"])]
        [r] = wakes.wake_store().read()
        assert r["status"] == "done"
        assert turn_budget.turns_today() == 0   # a push is not an autonomous turn

    def test_a_console_reminder_goes_to_the_personal_chat(self, monkeypatch):
        from meeseek.config import cfg
        from meeseek.runtime import inbox

        ch = self._channel(monkeypatch)
        monkeypatch.setattr(inbox, "by_session", lambda sid: None)
        monkeypatch.setattr(cfg.triggers, "coach_telegram_chat", "me1")
        _file(kind="remind", session_id="console-1", payload={"text": "開會"})
        asyncio.run(wakes.poll())
        assert ch.sent == [("me1", ["⏰ 開會"])]

    def test_the_quiet_hours_hold_the_reminder_until_they_end(self, monkeypatch):
        from meeseek.runtime import messengers

        ch = self._channel(monkeypatch)
        until = time.time() + 3600
        monkeypatch.setattr(messengers, "quiet_until", lambda now=None: until)
        _file(kind="remind", payload={"text": "x"})
        asyncio.run(wakes.poll())
        assert ch.sent == []
        [r] = wakes.wake_store().read()
        assert r["status"] == "pending" and r["rearmed"] == 1
        assert abs(r["due_at"] - until) < 5

    def test_without_a_chat_to_push_to_it_fails(self, monkeypatch):
        from meeseek.config import cfg
        from meeseek.runtime import inbox

        monkeypatch.setattr(inbox, "by_session", lambda sid: None)
        monkeypatch.setattr(cfg.triggers, "coach_telegram_chat", "")
        _file(kind="remind", payload={"text": "x"})
        asyncio.run(wakes.poll())
        [r] = wakes.wake_store().read()
        assert r["status"] == "failed"


class TestParseWhen:
    def test_relative_times(self):
        now = time.time()
        assert wakes.parse_when("15m", now) == now + 900
        assert wakes.parse_when("2h", now) == now + 7200
        assert wakes.parse_when("90s", now) == now + 90
        assert wakes.parse_when("1d", now) == now + 86400

    def test_a_clock_time_still_to_come_is_today(self):
        from datetime import datetime

        noon = datetime(2026, 5, 1, 12, 0).astimezone().timestamp()
        assert wakes.parse_when("17:00", noon) == datetime(2026, 5, 1, 17, 0).astimezone().timestamp()

    def test_a_clock_time_already_past_is_tomorrow(self):
        from datetime import datetime

        noon = datetime(2026, 5, 1, 12, 0).astimezone().timestamp()
        assert wakes.parse_when("09:30", noon) == datetime(2026, 5, 2, 9, 30).astimezone().timestamp()

    def test_a_full_date_in_the_future_is_taken_as_is(self):
        from datetime import datetime

        noon = datetime(2026, 5, 1, 12, 0).astimezone().timestamp()
        assert wakes.parse_when("2026-05-03 09:30", noon) == datetime(2026, 5, 3, 9, 30).astimezone().timestamp()

    def test_a_full_date_in_the_past_is_refused(self):
        import pytest

        from datetime import datetime

        noon = datetime(2026, 5, 1, 12, 0).astimezone().timestamp()
        with pytest.raises(ValueError, match="已經過了"):
            wakes.parse_when("2026-04-30 09:30", noon)

    def test_nonsense_is_refused(self):
        import pytest

        for bad in ("soon", "25:00", "2026-13-01 09:30", ""):
            with pytest.raises(ValueError):
                wakes.parse_when(bad, time.time())


class TestRemindMe:
    def test_the_tool_files_a_wake_for_the_asking_session(self):
        said = wakes.remind_me("15m", "喝水", _origin={"user": "inbox", "session_id": "s1"})
        assert "提醒你" in said and "喝水" in said
        [w] = wakes.pending("s1")
        assert w["kind"] == "remind" and w["payload"] == {"text": "喝水", "user": "inbox"}
        assert time.time() + 850 < w["due_at"] < time.time() + 950

    def test_a_bad_time_files_nothing_and_says_why(self):
        said = wakes.remind_me("soon", "x", _origin={"user": "webui", "session_id": "s1"})
        assert said.startswith("沒有排提醒")
        assert wakes.pending() == []
