"""Every conversation meeseek has had, from either engine, as one list of rows.

The past-sessions tools, the usage page, the nightly reading of what holds of
you, the case library and the Inbox read conversations back. A row is one
event in the shape ADK stores (`author`, `content.parts`, `usage_metadata`,
snake_case), whichever engine wrote it:

    ADK          .cache/sessions.db — plain SQLite with JSON, read without ADK,
                 so the conversations from before the switch stay readable
    meeseek's    the event store (engine.py), each event put in that shape

A text-only model step brings its token usage but not its text: the reply
that follows is the text, once, as shipped (a draft a policy sent back is not
what was said).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from meeseek.core.events import Event, ModelStepped, Replied, ToolHeld, ToolRan, UserSaid


@dataclass(frozen=True)
class Row:
    user: str
    session: str
    turn: str
    at: float
    event: dict


def record(e: Event) -> dict | None:
    """One engine event as ADK stores one; None for what is not said or done."""
    base = {"author": e.by, "invocation_id": e.turn, "timestamp": e.at, "partial": False}
    if isinstance(e, UserSaid):
        return {**base, "author": "user", "content": {"role": "user", "parts": [{"text": e.text}]}}
    if isinstance(e, ModelStepped):
        parts = [{"function_call": {"id": c.id, "name": c.name, "args": c.args}} for c in e.calls]
        if e.text and e.calls:
            parts.insert(0, {"text": e.text})
        out = {**base, "content": {"role": "model", "parts": parts}}
        if e.usage is not None:
            out["model_version"] = e.usage.model
            out["usage_metadata"] = {"prompt_token_count": e.usage.input_tokens,
                                     "candidates_token_count": e.usage.output_tokens,
                                     "cached_content_token_count": e.usage.cached_tokens}
        return out if parts or e.usage is not None else None
    if isinstance(e, (ToolRan, ToolHeld)):
        response = ({"result": e.reason, "held": True} if isinstance(e, ToolHeld)
                    else e.result if isinstance(e.result, dict) else {"result": e.result})
        return {**base, "content": {"role": "user", "parts": [
            {"function_response": {"id": e.call.id, "name": e.call.name, "response": response}}]}}
    if isinstance(e, Replied):
        return {**base, "content": {"role": "model", "parts": [{"text": e.text}]}}
    return None


def _legacy(db: Path, since: float, session: str, users: tuple[str, ...]) -> list[Row]:
    from meeseek.runtime.sessions import APP_NAME

    if not db.exists():
        return []
    sql = "SELECT user_id, session_id, invocation_id, timestamp, event_data FROM events WHERE app_name = ? AND timestamp >= ?"
    args: list = [APP_NAME, since]
    if session:
        sql += " AND session_id = ?"
        args.append(session)
    if users:
        sql += f" AND user_id IN ({','.join('?' * len(users))})"
        args += users
    try:
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        fetched = conn.execute(sql + " ORDER BY timestamp", args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = []
    for user, sid, inv, ts, data in fetched:
        try:
            event = json.loads(data)
        except ValueError:
            continue
        out.append(Row(user=user, session=sid, turn=inv or "", at=ts, event=event))
    return out


def _engine(since: float, session: str, users: tuple[str, ...]) -> list[Row]:
    from meeseek import engine

    store = engine.store()
    if not store.path.exists():
        return []
    out = []
    for sid in ([session] if session else store.sessions()):
        events = store.read(sid)
        owner = next((e.by for e in events if isinstance(e, UserSaid)), "")
        if users and owner not in users:
            continue
        for e in events:
            if e.at >= since and (rec := record(e)) is not None:
                out.append(Row(user=owner, session=sid, turn=e.turn, at=e.at, event=rec))
    return out


def rows(*, since: float = 0.0, session: str = "", users: Iterable[str] = (), db: Path | None = None) -> list[Row]:
    """Rows since `since`, oldest first: one session's, or those of `users`, or all.
    `db` points the ADK half at another file (tests)."""
    from meeseek.config.paths import home

    who = tuple(users)
    return sorted([*_legacy(db or home().sessions_db, since, session, who), *_engine(since, session, who)],
                  key=lambda r: r.at)


def legacy_sessions(user: str, db: Path | None = None) -> list[tuple[str, float]]:
    """(session id, last update) of `user`'s conversations from before the
    engine switch, in ADK's sessions.db."""
    from meeseek.config.paths import home
    from meeseek.runtime.sessions import APP_NAME

    path = db or home().sessions_db
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            return [(sid, ts) for sid, ts in conn.execute(
                "SELECT id, update_time FROM sessions WHERE app_name = ? AND user_id = ?", (APP_NAME, user))]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def legacy_events(session: str, user: str = "", db: Path | None = None) -> list[Event]:
    """A conversation from before the engine switch, as the engine's events;
    the person's messages are by `user` (the session's owner), as the engine
    writes them."""
    from meeseek.config.paths import home

    found = _legacy(db or home().sessions_db, 0.0, session, (user,) if user else ())
    owner = user or (found[0].user if found else "")
    return [e.model_copy(update={"by": owner}) if isinstance(e, UserSaid) and owner else e
            for e in to_core([r.event for r in found], session)]


def delete_legacy(session: str, user: str, db: Path | None = None) -> None:
    """Forget a conversation from before the engine switch (the console's delete)."""
    from meeseek.config.paths import home
    from meeseek.runtime.sessions import APP_NAME

    path = db or home().sessions_db
    if not path.exists():
        return
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute("DELETE FROM events WHERE app_name = ? AND user_id = ? AND session_id = ?",
                         (APP_NAME, user, session))
            conn.execute("DELETE FROM sessions WHERE app_name = ? AND user_id = ? AND id = ?", (APP_NAME, user, session))
    finally:
        conn.close()


def text_of(event: dict) -> str:
    """The text a row says (its thoughts left out)."""
    parts = (event.get("content") or {}).get("parts") or []
    return "".join(p.get("text") or "" for p in parts if not p.get("thought"))


def _parts(event: dict) -> list[dict]:
    return (event.get("content") or {}).get("parts") or []


def pending_question(found: list[Row]):
    """The last ask_user call nobody has answered in these rows, as an ask.Question."""
    from meeseek.runtime.ask import TOOL_NAME, Question

    answered: set[str] = set()
    for r in reversed(found):
        author = r.event.get("author")
        for p in _parts(r.event):
            fr, fc = p.get("function_response"), p.get("function_call")
            if fr and fr.get("name") == TOOL_NAME and author == "user":
                answered.add(fr.get("id"))
            if fc and fc.get("name") == TOOL_NAME:
                if fc.get("id") in answered:
                    return None
                args = fc.get("args") or {}
                return Question(call_id=fc.get("id") or "", invocation_id=r.turn, question=str(args.get("question", "")),
                                options=[str(o) for o in args.get("options") or []],
                                recommended=str(args.get("recommended", "")))
        if author == "user" and any(p.get("text") for p in _parts(r.event)):
            return None                 # the person has spoken since: nothing is waiting
    return None


def turn_status(found: list[Row], turn: str) -> str:
    """`done`, `waiting` (on an ask_user answer), `interrupted`, or `unknown` (no such turn)."""
    mine = [r for r in found if r.turn == turn and not r.event.get("partial")]
    if not mine:
        return "unknown"
    question = pending_question(found)
    if question is not None and question.invocation_id == turn:
        return "waiting"
    for r in reversed(mine):
        parts = _parts(r.event)
        if r.event.get("author") == "user" or not parts:
            continue
        if any(p.get("function_call") or p.get("function_response") for p in parts):
            return "interrupted"
        if any(p.get("text") and not p.get("thought") for p in parts):
            return "done"
    return "interrupted"


def to_core(records: list[dict], session: str) -> list[Event]:
    """Events stored in ADK's shape, read as the engine's own: how a replay is
    seeded with the conversation a case was cut from."""
    from meeseek.core.events import ToolCall
    from meeseek.core.ledger import failure_of

    out: list[Event] = []
    open_calls: dict[str, ToolCall] = {}
    n = 0
    for r in records:
        turn, at, by = r.get("invocation_id") or "", r.get("timestamp") or 0.0, r.get("author") or ""
        base = {"session": session, "turn": turn, "at": at, "by": by}
        parts = _parts(r)
        responses = [p["function_response"] for p in parts if p.get("function_response")]
        if responses:
            for fr in responses:
                call = open_calls.pop(fr.get("id") or "", None) or next(
                    (open_calls.pop(k) for k, c in list(open_calls.items()) if c.name == fr.get("name")), None) \
                    or ToolCall(id=fr.get("id") or f"call-{n}", name=fr.get("name") or "")
                resp = fr.get("response")
                if isinstance(resp, dict) and resp.get("held"):
                    out.append(ToolHeld(**base, call=call, reason=str(resp.get("result", ""))))
                else:
                    out.append(ToolRan(**base, call=call, result=resp, failed=bool(failure_of(resp))))
            continue
        text = text_of(r).strip()
        if by == "user":
            if text:
                out.append(UserSaid(**base, text=text))
            continue
        calls = []
        for p in parts:
            fc = p.get("function_call")
            if fc:
                n += 1
                call = ToolCall(id=fc.get("id") or f"call-{n}", name=fc.get("name") or "", args=fc.get("args") or {})
                open_calls[call.id] = call
                calls.append(call)
        if text or calls:
            out.append(ModelStepped(**base, text=text, calls=tuple(calls)))
        if text and not calls:
            out.append(Replied(**base, text=text))
    return out


def eval_turns(records: list[dict]) -> list[tuple[str, dict]]:
    """(turn, the turn as an eval invocation) for each turn of a conversation
    in stored shape: its user line, final reply and tool calls — the shape
    ADK's convert_session_to_eval_invocations gives, which a rule's case keeps."""
    order: list[str] = []
    by_turn: dict[str, dict] = {}
    for r in records:
        turn = r.get("invocation_id") or ""
        if not turn:
            continue
        if turn not in by_turn:
            order.append(turn)
            by_turn[turn] = {"invocation_id": turn, "user_content": {"role": "user", "parts": []},
                             "final_response": {"role": "model", "parts": []},
                             "intermediate_data": {"tool_uses": []}}
        inv = by_turn[turn]
        text = text_of(r).strip()
        if r.get("author") == "user":
            if text and not inv["user_content"]["parts"]:
                inv["user_content"]["parts"] = [{"text": text}]
            continue
        for p in _parts(r):
            if p.get("function_call"):
                fc = p["function_call"]
                inv["intermediate_data"]["tool_uses"].append({"id": fc.get("id"), "name": fc.get("name"),
                                                               "args": fc.get("args") or {}})
        if text:
            inv["final_response"]["parts"] = [{"text": text}]
    return [(t, by_turn[t]) for t in order]
