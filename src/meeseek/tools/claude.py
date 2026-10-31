"""The tools an agent hands work to Claude Code with (runtime/claude_agent.py)."""

from __future__ import annotations

import time

from meeseek.runtime import claude_agent

_WAIT_MAX = 900


def _ago(ts: float) -> str:
    s = max(0, time.time() - ts)
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


def claude_sessions(query: str = "", limit: int = 10) -> str:
    """The Claude Code sessions on this machine, newest first."""
    try:
        rows = claude_agent.sessions(query, limit=max(1, min(int(limit or 10), 30)))
    except Exception as e:  # noqa: BLE001
        return f"Could not list Claude sessions: {e}"
    if not rows:
        return "No Claude sessions" + (f" matching {query!r}." if query else ".")
    lines = []
    for n, r in enumerate(rows, 1):
        who = "meeseek" if r["by_meeseek"] else "you in Claude Desktop" if r.get("desktop") else "you"
        status = f" · {r['status']}" if r["status"] else ""
        branch = f" ({r['branch']})" if r["branch"] else ""
        lines.append(f"{n}. {r['title']} — {r['cwd']}{branch} · {_ago(r['updated_at'])} · started by {who}{status}\n"
                     f"   session_id: {r['session_id']}")
    return ("Show the user this numbered list (title, folder, when) so they can pick one; keep the "
            "session_ids for yourself.\n\n" + "\n".join(lines))


def _report(record: dict) -> str:
    sid = record.get("session_id", "")
    if claude_agent.running(record["id"]):
        return (f"Claude is still working (session_id {sid or 'starting'}). Tell the user it will report "
                "back when it is done — the answer reaches them where they asked — or check again with check_claude.")
    last = next((h for h in reversed(record.get("handoffs", [])) if h["from"] == "claude"), {})
    head = f"[Claude session {sid} · {record['cwd']}"
    if last.get("turns"):
        head += f" · {last['turns']} turns"   # no money: it runs on the Claude Code subscription
    head += "]"
    if last.get("error"):
        head += f"\n(ended with: {last['error']})"
    return f"{head}\n\n{last.get('text', '(no reply)')}"


def ask_claude(prompt: str, cwd: str = "", session_id: str = "", wait_seconds: int = 240,
               _origin: dict | None = None) -> str:
    """Hand work to Claude Code and wait up to `wait_seconds` for its answer."""
    wait = max(5, min(int(wait_seconds or 240), _WAIT_MAX))
    try:
        record = claude_agent.start(prompt, cwd=cwd, session_id=session_id, origin=_origin, awaited_for=wait)
    except ValueError as e:
        return f"Claude did not start: {e}"
    record = claude_agent.wait(record["id"], wait)
    if claude_agent.running(record["id"]):
        _file_followup(record, _origin)
    return _report(record)


def _file_followup(record: dict, origin: dict | None) -> None:
    """Claude outlasted the wait: a wake on disk (runtime/wakes.py) takes the
    result over with a turn on the asking session when Claude ends — or at the
    timeout — so "it will report back" is a promise something keeps."""
    from meeseek.runtime import wakes

    session_id = (origin or {}).get("session_id", "")
    if not session_id:
        return
    wakes.file(kind="claude_followup", due_at=time.time() + wakes.FOLLOWUP_TIMEOUT_S,
               waiting_on=f"claude:{record['id']}",
               why=f"Claude 還在做「{record.get('title', '')}」；結束後接手處理結果",
               session_id=session_id,
               payload={"record_id": record["id"], "user": (origin or {}).get("user", "")},
               key=f"claude:{record['id']}")


def claude_check(session_id: str, wait_seconds: int = 0) -> str:
    """Where a Claude session meeseek started stands, and its latest answer."""
    record = claude_agent.record_of(session_id)
    if record is None:
        return ("meeseek has not run this session. Continue it with ask_claude(session_id=…) "
                "to hand it the next step.")
    if wait_seconds:
        record = claude_agent.wait(record["id"], max(1, min(int(wait_seconds), _WAIT_MAX)))
    return _report(record)
