"""Tasks: a goal on disk that outlives the turn, pushed forward by its wake."""

import asyncio
import time

from meeseek.runtime import tasks, wakes


def _task(**kw):
    kw.setdefault("goal", "追 MR !169 到合併")
    kw.setdefault("success", "MR !169 已 merge")
    kw.setdefault("session_id", "chat-1")
    kw.setdefault("user_id", "webui")
    return tasks.create(**kw)


CONSOLE = {"user": "webui", "session_id": "chat-1"}
TRIGGER = {"user": "inbox", "session_id": "inbox-x1"}


def _due_now(task_id):
    """Pull the task's pending check into the past, so the next poll claims it."""
    def fn(records):
        for r in records:
            if r.get("key") == f"task_check:{task_id}" and r["status"] == "pending":
                r["due_at"] = time.time() - 1
        return records

    wakes.wake_store().update(fn)


class TestStore:
    def test_a_created_task_is_read_back_whole(self):
        t = _task()
        got = tasks.get(t["id"])
        assert (got["goal"], got["status"], got["session_id"]) == (t["goal"], "active", "chat-1")

    def test_progress_appends_and_is_kept_for_good(self):
        t = _task()
        for i in range(tasks.NOTES_KEPT + 5):
            tasks.add_note(t["id"], progress=f"step {i}")
        assert len(tasks.get(t["id"])["progress"]) == tasks.NOTES_KEPT + 5

    def test_only_the_newest_notes_are_kept(self):
        t = _task()
        for i in range(tasks.NOTES_KEPT + 5):
            tasks.add_note(t["id"], note=f"note {i}")
        notes = tasks.get(t["id"])["notes"]
        assert len(notes) == tasks.NOTES_KEPT
        assert notes[-1]["text"] == f"note {tasks.NOTES_KEPT + 4}"

    def test_open_tasks_match_the_session_or_the_user(self):
        a = _task(session_id="chat-1", user_id="webui")
        b = _task(session_id="chat-2", user_id="webui")
        tasks.set_status(_task(session_id="chat-3", user_id="webui")["id"], "done")
        by_session = tasks.open_tasks(session_id="chat-1")
        assert [t["id"] for t in by_session] == [a["id"]]
        by_user = tasks.open_tasks(user_id="webui")
        assert {t["id"] for t in by_user} == {a["id"], b["id"]}

    def test_old_settled_tasks_are_pruned(self):
        t = _task()
        tasks.set_status(t["id"], "dropped")
        assert tasks.prune(now=time.time() + (tasks.KEEP_DAYS + 1) * 86400) == 1
        assert tasks.get(t["id"]) is None


class TestWake:
    def test_a_task_carries_at_most_one_pending_check(self):
        t = _task()
        first = tasks.schedule_check(t, hours=1)
        second = tasks.schedule_check(t, hours=2)
        assert first["id"] != second["id"]
        pending = [w for w in wakes.pending() if w["kind"] == "task_check"]
        assert [w["id"] for w in pending] == [second["id"]]
        assert tasks.next_check(t["id"]) == second["due_at"]

    def test_settling_the_task_cancels_its_check(self):
        t = _task()
        tasks.schedule_check(t, hours=1)
        tasks.task_update(t["id"], status="done", _origin=CONSOLE)
        assert tasks.next_check(t["id"]) == 0.0

    def test_the_check_turn_runs_on_the_tasks_session(self, monkeypatch):
        from meeseek.runtime import mcp_server

        t = _task()
        tasks.add_note(t["id"], progress="已找到 MR，CI 綠燈")
        turns = []

        async def run(request, session_id, *, user_id):
            turns.append((request, session_id, user_id))
            return "", "", []

        monkeypatch.setattr(mcp_server, "run_orchestrator_turn", run)
        tasks.schedule_check(t, hours=1)
        _due_now(t["id"])
        asyncio.run(wakes.poll())
        [(request, session_id, user_id)] = turns
        assert t["goal"] in request and "已找到 MR" in request and t["id"] in request
        assert (session_id, user_id) == ("chat-1", "webui")
        [w] = [w for w in wakes.wake_store().read() if w["kind"] == "task_check"]
        assert w["status"] == "done"
        from meeseek.runtime import turn_budget
        assert turn_budget.turns_today() == 1   # the check turn drew on the daily budget

    def test_a_settled_tasks_check_runs_no_turn(self, monkeypatch):
        from meeseek.runtime import mcp_server

        async def run(*a, **k):
            raise AssertionError("no turn for a settled task")

        monkeypatch.setattr(mcp_server, "run_orchestrator_turn", run)
        t = _task()
        tasks.schedule_check(t, hours=1)
        _due_now(t["id"])
        tasks.set_status(t["id"], "done")
        asyncio.run(wakes.poll())
        [w] = [w for w in wakes.wake_store().read() if w["kind"] == "task_check"]
        assert w["status"] == "done" and "already done" in w["note"]

    def test_a_gone_tasks_check_fails(self):
        t = _task()
        tasks.schedule_check(t, hours=1)
        _due_now(t["id"])
        tasks._path(t["id"]).unlink()
        asyncio.run(wakes.poll())
        [w] = [w for w in wakes.wake_store().read() if w["kind"] == "task_check"]
        assert w["status"] == "failed"

    def test_a_check_cannot_come_sooner_than_the_floor(self):
        t = _task()
        w = tasks.schedule_check(t, hours=0.01)   # 36 seconds: a self-spinning task
        assert w["due_at"] >= time.time() + tasks.MIN_CHECK_HOURS * 3600 - 5

    def test_past_the_daily_cap_the_check_waits_for_tomorrow(self, monkeypatch):
        from meeseek.config import cfg
        from meeseek.runtime import mcp_server, turn_budget

        monkeypatch.setattr(cfg.triggers, "daily_turns", 1)
        turn_budget.count_turn()   # the day's budget is already spent

        async def run(*a, **k):
            raise AssertionError("no turn past the daily cap")

        monkeypatch.setattr(mcp_server, "run_orchestrator_turn", run)
        t = _task()
        tasks.schedule_check(t, hours=1)
        _due_now(t["id"])
        asyncio.run(wakes.poll())
        [w] = [w for w in wakes.wake_store().read() if w["kind"] == "task_check"]
        assert w["status"] == "pending" and "明天" in w["note"]
        assert w["due_at"] >= time.time() + turn_budget.seconds_until_reset() - 60


class TestTools:
    def test_create_opens_a_task_with_its_first_check(self):
        out = tasks.task_create("追 MR", "已 merge", first_check_hours=24, _origin=CONSOLE)
        assert "開了任務" in out
        [t] = tasks.open_tasks(user_id="webui")
        assert (t["goal"], t["session_id"]) == ("追 MR", "chat-1")
        assert tasks.next_check(t["id"]) > time.time() + 23 * 3600

    def test_a_trigger_turn_cannot_open_a_task(self):
        out = tasks.task_create("追 MR", "已 merge", _origin=TRIGGER)
        assert "沒有執行" in out
        assert tasks.open_tasks(user_id="inbox") == []
        assert tasks.task_create("追 MR", "已 merge", _origin=None).startswith("[系統] 沒有執行")

    def test_a_trigger_turn_cannot_settle_a_task(self):
        t = _task()
        out = tasks.task_update(t["id"], status="done", _origin=TRIGGER)
        assert "沒有執行" in out
        assert tasks.get(t["id"])["status"] == "active"

    def test_a_trigger_turn_may_still_wait_and_reschedule(self):
        t = _task()
        out = tasks.task_update(t["id"], status="waiting", note="等 CI", next_check_hours=2,
                                _origin=TRIGGER)
        assert "waiting" in out
        got = tasks.get(t["id"])
        assert got["status"] == "waiting" and got["notes"][-1]["text"] == "等 CI"
        assert tasks.next_check(t["id"]) > time.time() + 3600

    def test_note_requires_something_to_note(self):
        t = _task()
        assert "至少" in tasks.task_note(t["id"])
        assert "沒有這個任務" in tasks.task_note("nope", note="x")
        assert "已記下" in tasks.task_note(t["id"], note="試了 A", progress="A 成功")

    def test_an_unknown_status_is_refused(self):
        t = _task()
        assert "沒有這個狀態" in tasks.task_update(t["id"], status="paused", _origin=CONSOLE)


class TestInstruction:
    def test_no_open_task_means_no_block(self):
        assert tasks.instruction(session_id="chat-9", user_id="nobody") == ""

    def test_the_block_carries_goal_progress_and_notes(self):
        t = _task()
        tasks.add_note(t["id"], note="CI 還在跑", progress="MR 已 approve")
        tasks.schedule_check(t, hours=4)
        block = tasks.instruction(session_id="chat-1", user_id="webui")
        for piece in (t["id"], t["goal"], t["success"], "MR 已 approve", "CI 還在跑", "下次檢查"):
            assert piece in block

