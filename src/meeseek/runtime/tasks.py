"""Tasks — a goal that outlives the turn it was agreed in.

The goal/plan split: the GOAL lives in the Task — what the person
agreed to (`goal`), how we know it is done (`success`), what has worked so
far (`progress`, append-only) and the agent's working notes (`notes`, the
newest NOTES_KEPT) — and survives any number of turns. The PLAN lives only
inside one turn: each check re-derives it from the task and the world, so a
stale plan dies with its turn.

A Task is opened and settled only from a turn the person drives in the
console (CONSOLE_USERS); a trigger turn (Inbox, MCP, workflow) can neither
open nor close one — it may only note progress. A task usually carries one
wake (runtime/wakes.py, kind "task_check"): when it comes due, an
orchestrator turn on the task's own session sees the task in its context
block (blocks/tasks), pushes it forward, notes what happened and books the
next check — or settles the task and reports.

Tasks are one JSON file each under tasks_dir(), like the Inbox's items.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from meeseek.config.paths import home
from meeseek.context import prompts
from meeseek.runtime.store import path_lock


def tasks_dir() -> Path:
    return home().data / "tasks"


KEEP_DAYS = 30          # settled tasks stay this long, for the record
NOTES_KEPT = 10         # the newest notes a task keeps; older ones fall off
MAX_IN_CONTEXT = 5      # open tasks one turn is shown
MIN_CHECK_HOURS = 0.25  # no check comes sooner: a task cannot spin itself in seconds

OPEN = ("active", "waiting")
SETTLED = ("done", "dropped")
STATUSES = OPEN + SETTLED

# Users whose turns the person drives in the console — the only turns that may
# open or settle a task. "webui" is the console (frontend/src/lib/api.ts);
# "user" is what a wake's follow-up turn falls back to (runtime/wakes.py).
CONSOLE_USERS = ("webui", "user")


# --- the records -----------------------------------------------------------------

def _path(task_id: str) -> Path:
    return tasks_dir() / f"{re.sub(r'[^a-f0-9]', '', task_id)}.json"


def save(task: dict) -> None:
    tasks_dir().mkdir(parents=True, exist_ok=True)
    with path_lock(_path(task["id"])):
        task["updated_at"] = time.time()
        _path(task["id"]).write_text(json.dumps(task, ensure_ascii=False, indent=1), encoding="utf-8")


def get(task_id: str) -> dict | None:
    p = _path(task_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _all() -> list[dict]:
    if not tasks_dir().exists():
        return []
    out = []
    for p in tasks_dir().glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue  # half-written; the next read sees it whole
    return out


def _must(task_id: str) -> dict:
    task = get(task_id)
    if task is None:
        raise ValueError(f"no such task: {task_id}")
    return task


def create(*, goal: str, success: str, session_id: str, user_id: str) -> dict:
    now = time.time()
    task = {"id": uuid.uuid4().hex[:10], "goal": goal.strip(), "success": success.strip(),
            "status": "active", "progress": [], "notes": [],
            "session_id": session_id, "user_id": user_id,
            "created_at": now, "updated_at": now}
    save(task)
    return task


def open_tasks(*, session_id: str = "", user_id: str = "") -> list[dict]:
    """Every open task of this session or this user, oldest first."""
    rows = [t for t in _all() if t["status"] in OPEN
            and ((session_id and t["session_id"] == session_id)
                 or (user_id and t["user_id"] == user_id))]
    return sorted(rows, key=lambda t: t["created_at"])


def add_note(task_id: str, note: str = "", progress: str = "") -> dict:
    """Append to the task's record: `progress` is a step that worked (kept for
    good), `note` is what was tried and seen (the newest NOTES_KEPT stay)."""
    task = _must(task_id)
    now = time.time()
    if progress.strip():
        task["progress"].append({"at": now, "text": progress.strip()})
    if note.strip():
        task["notes"].append({"at": now, "text": note.strip()})
        del task["notes"][:-NOTES_KEPT]
    save(task)
    return task


def set_status(task_id: str, status: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"not a task status: {status!r}")
    task = _must(task_id)
    task["status"] = status
    save(task)
    return task


def prune(now: float | None = None) -> int:
    """Drop settled tasks older than KEEP_DAYS."""
    cutoff = (now or time.time()) - KEEP_DAYS * 86400
    gone = 0
    for task in _all():
        if task["status"] in SETTLED and task["updated_at"] < cutoff:
            _path(task["id"]).unlink(missing_ok=True)
            gone += 1
    return gone


# --- the wake that checks on it (runtime/wakes.py) --------------------------------

def _check_key(task_id: str) -> str:
    return f"task_check:{task_id}"


def schedule_check(task: dict, hours: float) -> dict:
    """Book the task's next check as a wake; an earlier pending check is
    settled — a task carries at most one. `hours` under MIN_CHECK_HOURS books
    MIN_CHECK_HOURS: the floor under how fast a task may re-arm itself."""
    from meeseek.runtime import wakes

    hours = max(float(hours), MIN_CHECK_HOURS)
    cancel_check(task["id"], note="superseded by a new check time")
    return wakes.file(kind="task_check", due_at=time.time() + hours * 3600,
                      session_id=task["session_id"], payload={"task_id": task["id"]},
                      why=f"任務 [{task['id']}] 的下次檢查", key=_check_key(task["id"]))


def cancel_check(task_id: str, note: str = "task settled") -> None:
    from meeseek.runtime import wakes

    for w in wakes.pending():
        if w.get("key") == _check_key(task_id):
            wakes.settle(w["id"], "done", note)


def next_check(task_id: str) -> float:
    """When the task's pending check is due; 0.0 if none is booked."""
    from meeseek.runtime import wakes

    w = next((w for w in wakes.pending() if w.get("key") == _check_key(task_id)), None)
    return w["due_at"] if w else 0.0


# --- what a turn sees --------------------------------------------------------------

def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def describe(task: dict) -> str:
    """The task as a turn reads it: goal, success, what worked, recent notes."""
    lines = [f"目標：{task['goal']}", f"成功條件：{task['success']}", f"狀態：{task['status']}"]
    if task["progress"]:
        lines.append("進度（成功的步驟）：")
        lines += [f"  {i}. {p['text']}（{_when(p['at'])}）"
                  for i, p in enumerate(task["progress"], start=1)]
    if task["notes"]:
        lines.append(f"最近筆記（最多 {NOTES_KEPT} 筆）：")
        lines += [f"  - {n['text']}（{_when(n['at'])}）" for n in task["notes"]]
    due = next_check(task["id"])
    if due:
        lines.append(f"下次檢查：{_when(due)}")
    return "\n".join(lines)


def instruction(*, session_id: str, user_id: str) -> str:
    """The per-turn task block: every open task of this session or user,
    rendered for the agent's dynamic instruction; "" when there is none."""
    rows = open_tasks(session_id=session_id, user_id=user_id)[:MAX_IN_CONTEXT]
    if not rows:
        return ""
    body = "\n\n".join(f"[{t['id']}]\n{describe(t)}" for t in rows)
    return prompts.text("blocks/tasks#open", tasks=body)




# --- the tools (tools/registry.py) --------------------------------------------------

def _console(origin: dict | None) -> bool:
    return (origin or {}).get("user") in CONSOLE_USERS


def task_create(goal: str, success: str, first_check_hours: float = 24, _origin=None) -> str:
    if not _console(_origin):
        return prompts.text("runtime/tasks#not_console", action="建立")
    task = create(goal=goal, success=success,
                  session_id=_origin.get("session_id", ""), user_id=_origin["user"])
    when = ""
    if first_check_hours and first_check_hours > 0:
        wake = schedule_check(task, float(first_check_hours))
        when = f"，下次檢查 {_when(wake['due_at'])}"
    return f"✅ 開了任務 [{task['id']}]：{task['goal']}{when}"


def task_note(task_id: str, note: str = "", progress: str = "") -> str:
    task = get(task_id)
    if task is None:
        return f"沒有這個任務：{task_id}"
    if not (note.strip() or progress.strip()):
        return "沒有記下任何東西：note 和 progress 至少要給一個"
    task = add_note(task_id, note=note, progress=progress)
    return f"已記下。任務 [{task_id}] 目前有 {len(task['progress'])} 筆進度、{len(task['notes'])} 筆筆記"


def task_update(task_id: str, status: str = "", note: str = "",
                next_check_hours: float = 0, _origin=None) -> str:
    task = get(task_id)
    if task is None:
        return f"沒有這個任務：{task_id}"
    if status and status not in STATUSES:
        return f"沒有這個狀態：{status}（可用：{', '.join(STATUSES)}）"
    if status in SETTLED and not _console(_origin):
        return prompts.text("runtime/tasks#not_console", action="結案")
    if note.strip():
        add_note(task_id, note=note)
    if status:
        task = set_status(task_id, status)
    said = [f"任務 [{task_id}] 狀態：{task['status']}"]
    if task["status"] in SETTLED:
        cancel_check(task_id)
        said.append("已取消排定的檢查")
    elif next_check_hours and next_check_hours > 0:
        wake = schedule_check(task, float(next_check_hours))
        said.append(f"下次檢查 {_when(wake['due_at'])}")
    return "；".join(said)
