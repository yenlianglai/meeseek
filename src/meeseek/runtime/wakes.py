"""Wakes — a promise that something happens later, kept as a record on disk.

A wake says when to act (`due_at`), what it may be waiting for (`waiting_on`,
an event key such as "claude:<record id>" — the event sets it due at once,
and `due_at` is then only the timeout), why it exists (one line of hand-over
for whoever reads the record later) and what to do (`kind`, dispatched to a
handler). The scheduler polls every 30 seconds and runs the due ones.
Because the records are on disk, a restart changes nothing: the poll after
it picks up where the dead process left off — the opposite of a
`loop.call_later` timer, which a restart silently drops.

Kinds:
  inbox_retry     — re-run an Inbox item's turn (runtime/inbox.py files it)
  claude_followup — Claude Code outlasted the turn that asked: an
                    orchestrator turn on the asking session takes the result
                    over (tools/claude.py files it; runtime/claude_agent.py
                    fires the event)
  task_check      — a Task's next check came due: an orchestrator turn on
                    its session pushes it forward (runtime/tasks.py files it)
  quiet_push      — a Telegram push the quiet hours held, sent once they end
                    (runtime/messengers files it)
  remind          — the user's own「X 點叫我」: the text, pushed to their chat
                    when it comes due — no orchestrator turn, no turn budget
                    (the remind_me tool below files it)

The harness (runtime/harness.py) reads `pending(session_id)` to decide
whether a reply's "I'll report back" is a promise something will keep.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timedelta

from meeseek.config.paths import home
from meeseek.runtime.store import JsonlStore

logger = logging.getLogger(__name__)


def wake_store() -> JsonlStore:
    return JsonlStore(home().data / "wakes.jsonl")


KEEP_DAYS = 7        # settled wakes stay this long, for the record
MAX_ATTEMPTS = 3     # runs a crash interrupted; a failing handler settles itself
REARM_MAX = 6        # times a handler may push its own wake back
FOLLOWUP_TIMEOUT_S = 3600
CAP_RETRY_MARGIN_S = 300   # past midnight, not at it: the day's reset is settled


# --- the records -----------------------------------------------------------------

def file(*, kind: str, due_at: float, waiting_on: str = "", why: str = "",
         session_id: str = "", payload: dict | None = None, key: str = "") -> dict:
    """File one wake; with `key`, filing again while one is pending returns
    the wake already filed instead of a second one."""
    wake = {"id": uuid.uuid4().hex[:10], "key": key, "kind": kind, "payload": payload or {},
            "due_at": due_at, "waiting_on": waiting_on, "why": why, "session_id": session_id,
            "status": "pending", "attempts": 0, "rearmed": 0,
            "created_at": time.time(), "claimed_at": 0.0, "settled_at": 0.0, "note": ""}
    out = {"wake": wake}

    def fn(records):
        if key:
            same = next((r for r in records if r.get("key") == key and r["status"] == "pending"), None)
            if same is not None:
                out["wake"] = same
                return None
        return records + [wake]

    wake_store().update(fn)
    return out["wake"]


def pending(session_id: str = "") -> list[dict]:
    """Every wake still to come — for one session when `session_id` is given."""
    rows = [r for r in wake_store().read() if r["status"] == "pending"]
    if session_id:
        rows = [r for r in rows if r.get("session_id") == session_id]
    return rows


def has_pending(key: str) -> bool:
    return any(r.get("key") == key and r["status"] == "pending" for r in wake_store().read())


def exists(key: str) -> bool:
    """A wake with this key, in any state — settled ones count until pruned.
    How a delivery that a wake supersedes (messengers.claude_done) stands
    aside even if the wake has already run."""
    return any(r.get("key") == key for r in wake_store().read())


def on_event(waiting_on: str) -> int:
    """The event a wake was waiting for happened: set it due now. The next
    poll — at most 30 seconds away — runs it."""
    now = time.time()
    hit: list[str] = []

    def fn(records):
        for r in records:
            if r["status"] == "pending" and r.get("waiting_on") == waiting_on and r["due_at"] > now:
                r["due_at"] = now
                hit.append(r["id"])
        return records if hit else None

    wake_store().update(fn)
    return len(hit)


def claim_due(now: float | None = None) -> list[dict]:
    """Mark every due pending wake running, atomically, and return them.
    Settled wakes past KEEP_DAYS leave the store on the way."""
    now = now or time.time()
    claimed: list[dict] = []

    def fn(records):
        kept = []
        for r in records:
            if r["status"] in ("done", "failed") and now - (r["settled_at"] or r["created_at"]) > KEEP_DAYS * 86400:
                continue
            if r["status"] == "pending" and r["due_at"] <= now:
                r["status"], r["claimed_at"] = "running", now
                r["attempts"] += 1
                claimed.append(dict(r))
            kept.append(r)
        return kept

    wake_store().update(fn)
    return claimed


def settle(wake_id: str, status: str, note: str = "") -> None:
    def fn(records):
        for r in records:
            if r["id"] == wake_id:
                r["status"], r["settled_at"], r["note"] = status, time.time(), note[:500]
        return records

    wake_store().update(fn)


def rearm(wake: dict, seconds: float, note: str = "") -> None:
    """Push a claimed wake back: pending again, due later. A handler whose
    moment has not come (Claude still working at the timeout) uses this."""
    def fn(records):
        for r in records:
            if r["id"] == wake["id"]:
                r["status"], r["due_at"], r["claimed_at"] = "pending", time.time() + seconds, 0.0
                r["rearmed"] += 1
                r["note"] = note[:500]
        return records

    wake_store().update(fn)


def recover() -> int:
    """Wakes a restart left mid-run go back to pending — due now — unless they
    have already been interrupted MAX_ATTEMPTS times."""
    now = time.time()
    n = 0

    def fn(records):
        nonlocal n
        for r in records:
            if r["status"] != "running":
                continue
            if r["attempts"] >= MAX_ATTEMPTS:
                r["status"], r["settled_at"], r["note"] = "failed", now, "interrupted repeatedly"
            else:
                r["status"], r["due_at"], r["claimed_at"] = "pending", now, 0.0
            n += 1
        return records if n else None

    wake_store().update(fn)
    return n


# --- running them ----------------------------------------------------------------

async def poll() -> int:
    """Run every wake that has come due; how many were claimed. The scheduler
    calls this every 30 seconds. Handlers settle (or rearm) their own wake; one
    that raises instead is settled failed here."""
    n = 0
    for wake in claim_due():
        n += 1
        handler = _HANDLERS.get(wake["kind"])
        if handler is None:
            settle(wake["id"], "failed", f"no handler for kind {wake['kind']!r}")
            continue
        try:
            await handler(wake)
        except Exception as e:  # noqa: BLE001 — one wake's failure must not stop the rest
            logger.exception("[wakes] %s %s failed", wake["kind"], wake["id"])
            settle(wake["id"], "failed", f"{type(e).__name__}: {e}")
    return n


async def _inbox_retry(wake: dict) -> None:
    """Start the retry turn the Inbox put off (runtime/inbox.py)."""
    from meeseek.runtime import inbox

    item_id = wake["payload"].get("item_id", "")
    if inbox.get(item_id) is None:
        settle(wake["id"], "failed", f"no Inbox item {item_id}")
        return
    inbox.start(item_id)
    settle(wake["id"], "done", "retry started")


async def _claude_followup(wake: dict) -> None:
    """Claude Code finished (or ran past the timeout) after the turn that asked
    stopped waiting: one orchestrator turn on that session takes the result
    over — checks it against the task and reports to the person — instead of
    the raw reply being pushed at them."""
    from meeseek.context import prompts
    from meeseek.runtime import claude_agent, inbox

    record = claude_agent.get(wake["payload"].get("record_id", ""))
    if record is None:
        settle(wake["id"], "failed", "no such Claude record")
        return
    if record["status"] == "running" and claude_agent.running(record["id"]):
        if wake["rearmed"] < REARM_MAX:
            rearm(wake, FOLLOWUP_TIMEOUT_S, "Claude still working at the timeout")
            return
        status = f"超過 {REARM_MAX + 1} 個小時還在執行"
    else:
        status = "失敗了" if record["status"] == "failed" else "完成了"
    reply = claude_agent.last_reply(record) or "（還沒有回覆）"
    message = prompts.text("runtime/wakes#claude_followup", status=status,
                           title=record.get("title", ""), session_id=record.get("session_id", ""),
                           reply=reply[:6000])
    session_id = wake["session_id"]
    item = inbox.by_session(session_id)
    if item is not None:
        inbox.start(item["id"], message)      # its turn delivers to the phone too
    else:
        from meeseek.runtime.mcp_server import run_orchestrator_turn

        await run_orchestrator_turn(message, session_id,
                                    user_id=wake["payload"].get("user") or "user")
    settle(wake["id"], "done", f"follow-up turn on {session_id}")


async def _quiet_push(wake: dict) -> None:
    """Send the Telegram push the quiet hours held (runtime/messengers). The
    push checks the hours again itself: still quiet (the window moved), it
    files a fresh wake and this one is done all the same."""
    from meeseek.runtime import claude_agent, inbox, messengers

    push = wake["payload"].get("push", "")
    if push == "claude_done":
        record = claude_agent.get(wake["payload"].get("record_id", ""))
        if record is None:
            settle(wake["id"], "failed", "no such Claude record")
            return
        await messengers.claude_done(record)
    elif push in ("coach", "meeting_failed"):
        item = inbox.get(wake["payload"].get("item_id", ""))
        if item is None:
            settle(wake["id"], "failed", "no such Inbox item")
            return
        await (messengers.coach_push if push == "coach" else messengers.meeting_failed)(item)
    elif push == "digest":
        await messengers.inbox_digest()
    else:
        settle(wake["id"], "failed", f"unknown push {push!r}")
        return
    settle(wake["id"], "done", f"{push} sent after the quiet hours")


async def _task_check(wake: dict) -> None:
    """A task's check came due: one orchestrator turn on the task's own session
    — the task is in its context block — pushes it forward, notes what happened
    and books the next check, or settles the task and reports
    (runtime/tasks.py files these wakes).

    The turn draws on the same daily budget as the triggers'
    (`triggers.daily_turns`, runtime/turn_budget.py): past the cap the check is
    not dropped but pushed to just after midnight, when the count reads as 0
    again. A claude_followup turn is exempt — it only takes over the result of
    work a counted turn already started, and runs at most once per record."""
    from meeseek.context import prompts
    from meeseek.runtime import tasks, turn_budget

    task = tasks.get(wake["payload"].get("task_id", ""))
    if task is None:
        settle(wake["id"], "failed", "no such task")
        return
    if task["status"] not in tasks.OPEN:
        settle(wake["id"], "done", f"task already {task['status']}")
        return
    if turn_budget.cap_reached():
        rearm(wake, turn_budget.seconds_until_reset() + CAP_RETRY_MARGIN_S,
              "每日自主 turn 上限已滿（triggers.daily_turns），延到明天")
        return
    from meeseek.runtime.mcp_server import run_orchestrator_turn

    message = prompts.text("runtime/wakes#task_check", task_id=task["id"],
                           task=tasks.describe(task))
    turn_budget.count_turn()
    await run_orchestrator_turn(message, task["session_id"],
                                user_id=task["user_id"] or "user")
    settle(wake["id"], "done", f"check turn on {task['session_id']}")


async def _remind(wake: dict) -> None:
    """The reminder came due: its text, as a plain push to the chat that asked
    (runtime/messengers) — no orchestrator turn, and nothing drawn from the
    turn budget, because nothing thinks. Still inside the quiet hours (the
    window moved since it was booked), it waits for them to end like a
    quiet_push — unless it has waited REARM_MAX times already."""
    from meeseek.runtime import messengers

    until = messengers.quiet_until()
    if until and wake["rearmed"] < REARM_MAX:
        rearm(wake, until - time.time(), "安靜時段，提醒等時段結束再推")
        return
    if await messengers.remind_push(wake["session_id"], wake["payload"].get("text", "")):
        settle(wake["id"], "done", "reminder sent")
    else:
        settle(wake["id"], "failed", "no channel to push the reminder to")


_HANDLERS = {"inbox_retry": _inbox_retry, "claude_followup": _claude_followup,
             "quiet_push": _quiet_push, "task_check": _task_check, "remind": _remind}


# --- the remind tool (tools/registry.py) -------------------------------------------

_RELATIVE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd])", re.I)
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})")
_DATE_CLOCK = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_when(when: str, now: float | None = None) -> float:
    """`when` as the moment to remind, local time: relative ("15m", "2h" —
    s/m/h/d), a clock time ("17:00" — today, or tomorrow once it has passed),
    or a full date ("2026-10-28 09:30" — in the past it is refused).
    ValueError says what was wrong."""
    now = now if now is not None else time.time()
    s = (when or "").strip()
    if m := _RELATIVE.fullmatch(s):
        return now + float(m[1]) * _UNIT_S[m[2].lower()]
    if m := _CLOCK.fullmatch(s):
        hour, minute = int(m[1]), int(m[2])
        if hour > 23 or minute > 59:
            raise ValueError(f"「{s}」不是時刻（小時 0–23、分 0–59）")
        dt = datetime.fromtimestamp(now).astimezone().replace(hour=hour, minute=minute,
                                                              second=0, microsecond=0)
        if dt.timestamp() <= now:
            dt += timedelta(days=1)   # 已經過了的「17:00」是明天的五點
        return dt.timestamp()
    if m := _DATE_CLOCK.fullmatch(s):
        try:
            dt = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5])).astimezone()
        except ValueError:
            raise ValueError(f"「{s}」不是存在的日期時間") from None
        if dt.timestamp() <= now:
            raise ValueError(f"「{s}」已經過了——給相對時間（15m）或之後的時刻")
        return dt.timestamp()
    raise ValueError("要「15m」「2h」這樣的相對時間，或「17:00」「2026-10-28 09:30」")


def remind_me(when: str, text: str, _origin=None) -> str:
    try:
        due = parse_when(when)
    except ValueError as e:
        return f"沒有排提醒：{e}"
    origin = _origin or {}
    file(kind="remind", due_at=due, session_id=origin.get("session_id", ""),
         why=f"提醒：{text[:50]}", payload={"text": text, "user": origin.get("user", "")})
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(due))
    said = f"好，{stamp} 提醒你：「{text}」"
    from meeseek.runtime import messengers

    if until := messengers.quiet_until(due):
        said += f"（那時在安靜時段內，會等到 {time.strftime('%H:%M', time.localtime(until))} 才推）"
    return said
