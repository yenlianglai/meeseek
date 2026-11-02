"""Workstreams: one shared context per piece of work.

A workstream is a ticket, an MR or a branch — the same piece of work under any
of its names (a ticket's MR and branch are aliases of it). It keeps what every
conversation about that work needs, so a new meeseek turn or a Claude Code
session does not start from nothing:

    goal, state, next, decisions, open questions   (a fast-model summary, after each turn)
    events                                          (what happened: turns, Inbox news, merges)
    sessions, claude                                (the meeseek conversations and Claude Code
                                                     sessions that worked on it)

Who reads and writes it:

    engine.turn          a message that names one piece of work attaches its
                         conversation; every turn of an attached conversation
                         carries the context, and updates the summary after
    ask_claude           continues the workstream's Claude session, its brief
                         led by the context; the session's answer is noted
    triggers             Inbox news on the ticket or MR is an event; a merged MR
                         or a ticket you moved closes nothing by itself — a merge does
    workstream_get/note  the plugins: meeseek's agents and Claude Code read and
                         note it (and a Claude Code hook adds it to a prompt that
                         names the work, or runs on its branch)

A text that names more than two pieces of work (a sprint summary) attaches to
none: it is about many things, not one.

One JSON file each under workstreams_dir(). A closed workstream's summary is
saved to the knowledge base's notes.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from pydantic import BaseModel, Field

from meeseek.config.paths import home
from meeseek.runtime.store import path_lock

logger = logging.getLogger(__name__)

MAX_EVENTS = 30
MAX_DECISIONS = 10
MAX_QUESTIONS = 6
MAX_KEYS = 2          # a text naming more pieces of work than this is about many, not one

_JIRA = re.compile(r"(?<![A-Za-z0-9-])([A-Z][A-Z0-9]{1,9}-\d{1,6})(?![0-9A-Za-z])")   # a branch's "PROJ-12-fix" too
_MR_URL = re.compile(r"https?://[^\s/]+/((?:[\w.-]+/)+[\w.-]+)/-/merge_requests/(\d+)")
_MR_BANG = re.compile(r"(?<![\w!&])!(\d{1,6})\b")


def workstreams_dir() -> Path:
    return home().data / "workstreams"


def _enabled() -> bool:
    from meeseek.config import cfg

    return cfg.workstreams.enabled


# --- naming the work ------------------------------------------------------------

def _pack_config(name: str) -> dict:
    from meeseek.config import cfg

    s = next((s for s in cfg.mcp_servers if s.name == name), None)
    return dict(getattr(s, "config", None) or {}) if s else {}


def _ticket_projects() -> set[str]:
    """The Jira project keys a ticket key may have ({} = any)."""
    out = set()
    for p in _pack_config("jira").get("projects") or []:
        k = p.get("key") if isinstance(p, dict) else p
        if k:
            out.add(str(k).upper())
    return out


def _default_repo() -> str:
    repos = _pack_config("gitlab").get("projects") or []
    return str(repos[0]) if repos else ""


def keys_in(text: str) -> list[str]:
    """The pieces of work `text` names, in order: Jira keys ("PROJ-123"), MRs
    ("mr:<path>!<iid>" — a link, or "!123" in the default repo)."""
    text = text or ""
    projects = _ticket_projects()
    found: list[str] = []
    for m in _JIRA.finditer(text):
        k = m.group(1)
        if (not projects or k.split("-")[0] in projects) and k not in found:
            found.append(k)
    for m in _MR_URL.finditer(text):
        k = f"mr:{m.group(1)}!{m.group(2)}"
        if k not in found:
            found.append(k)
    repo = _default_repo()
    if repo:
        for m in _MR_BANG.finditer(text):
            k = f"mr:{repo}!{m.group(1)}"
            if k not in found:
                found.append(k)
    return found


def branch_key(branch: str) -> str:
    return f"branch:{branch.strip()}" if branch and branch.strip() else ""


def _slug(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", key).strip("-")[:120] or "work"


# --- the store --------------------------------------------------------------------

def _path(ws_id: str) -> Path:
    return workstreams_dir() / f"{_slug(ws_id)}.json"


def get(ws_id: str) -> dict | None:
    p = _path(ws_id)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None


def save(ws: dict) -> dict:
    workstreams_dir().mkdir(parents=True, exist_ok=True)
    ws["updated_at"] = time.time()
    p = _path(ws["id"])
    with path_lock(p):
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(ws, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    return ws


def _all() -> list[dict]:
    if not workstreams_dir().is_dir():
        return []
    out = []
    for p in workstreams_dir().glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def find(key: str) -> dict | None:
    """The workstream named `key` (its own key or an alias), an open one first."""
    if not key:
        return None
    hits = [w for w in _all() if key == w["key"] or key in w.get("aliases", [])]
    hits.sort(key=lambda w: (w.get("status") != "open", -w.get("updated_at", 0)))
    return hits[0] if hits else None


def _new(key: str, title: str = "") -> dict:
    now = time.time()
    ws = {"id": _slug(key), "key": key, "aliases": [], "title": title or key, "status": "open",
          "created_at": now, "updated_at": now, "goal": "", "state": "", "next": "",
          "decisions": [], "questions": [], "events": [], "sessions": [], "claude": [], "closed": None}
    while get(ws["id"]):                       # an older, closed one of the same name
        ws["id"] = f"{_slug(key)}-{int(now)}"
        now += 1
    return ws


def _alias(ws: dict, key: str) -> None:
    if key and key != ws["key"] and key not in ws["aliases"]:
        owner = find(key)
        if owner is None or owner["id"] == ws["id"] or owner.get("status") != "open":
            ws["aliases"].append(key)


def resolve(keys: list[str], *, create: bool = True, title: str = "") -> dict | None:
    """The one workstream `keys` name — an existing one, else (`create`) a new
    one under the first ticket key (or the first key); None for none or for
    more than MAX_KEYS (a text about many pieces of work)."""
    keys = list(dict.fromkeys(k for k in keys if k))
    if not keys or len(keys) > MAX_KEYS:
        return None
    ws = next((w for w in (find(k) for k in keys) if w and w.get("status") == "open"), None)
    if ws is None:
        if not create:
            return None
        primary = next((k for k in keys if not k.startswith(("mr:", "branch:"))), keys[0])
        ws = _new(primary, title)
    for k in keys:
        _alias(ws, k)
    return save(ws)


def for_session(session: str) -> dict | None:
    """The open workstream a meeseek conversation is attached to."""
    if not session:
        return None
    hits = [w for w in _all() if session in w.get("sessions", []) and w.get("status") == "open"]
    return max(hits, key=lambda w: w.get("updated_at", 0)) if hits else None


def for_claude(session_id: str) -> dict | None:
    if not session_id:
        return None
    hits = [w for w in _all() if any(c.get("session_id") == session_id for c in w.get("claude", []))
            and w.get("status") == "open"]
    return max(hits, key=lambda w: w.get("updated_at", 0)) if hits else None


def note(ws: dict, text: str, by: str = "", at: float | None = None) -> dict:
    """An event on the workstream, newest kept."""
    text = " ".join(str(text or "").split())[:400]
    if not text:
        return ws
    ws = get(ws["id"]) or ws
    ws["events"] = [*ws.get("events", []), {"at": at or time.time(), "by": by, "text": text}][-MAX_EVENTS:]
    return save(ws)


def attach_session(ws: dict, session: str) -> dict:
    if session and session not in ws.get("sessions", []):
        ws = get(ws["id"]) or ws
        ws["sessions"] = [*ws.get("sessions", []), session][-20:]
        ws = save(ws)
    return ws


def attach_claude(ws: dict, session_id: str, cwd: str = "", branch: str = "") -> dict:
    if not session_id:
        return ws
    ws = get(ws["id"]) or ws
    rows = [c for c in ws.get("claude", []) if c.get("session_id") != session_id]
    ws["claude"] = [*rows, {"session_id": session_id, "cwd": cwd, "at": time.time()}][-10:]
    if branch:
        _alias(ws, branch_key(branch))
    return save(ws)


def claude_session(ws: dict, cwd: str = "") -> str:
    """The workstream's latest Claude session — in `cwd` when one is named."""
    rows = sorted(ws.get("claude", []), key=lambda c: c.get("at", 0), reverse=True)
    for c in rows:
        if not cwd or Path(c.get("cwd") or "").resolve() == Path(cwd).expanduser().resolve():
            return c.get("session_id", "")
    return ""


# --- what a turn carries ------------------------------------------------------------

def render(ws: dict, chars: int | None = None) -> str:
    """The workstream as context: the summary, then the latest events."""
    from meeseek.config import cfg

    chars = chars or cfg.workstreams.context_chars
    names = ", ".join([ws["key"], *ws.get("aliases", [])])
    lines = [f"## Workstream: {ws.get('title') or ws['key']} ({names})",
             "Shared by meeseek and Claude Code: what is known about this piece of work so far."]
    for label, field in (("Goal", "goal"), ("State", "state"), ("Next", "next")):
        if ws.get(field):
            lines.append(f"- {label}: {ws[field]}")
    if ws.get("decisions"):
        lines.append("- Decisions: " + "; ".join(ws["decisions"]))
    if ws.get("questions"):
        lines.append("- Open questions: " + "; ".join(ws["questions"]))
    if ws.get("claude"):
        last = sorted(ws["claude"], key=lambda c: c.get("at", 0))[-1]
        lines.append(f"- Claude Code session: {last['session_id']} ({last.get('cwd') or '?'})")
    events = []
    for e in reversed(ws.get("events", [])):
        when = time.strftime("%m/%d %H:%M", time.localtime(e.get("at", 0)))
        events.append(f"  - {when} {e.get('by') or ''}: {e['text']}")
    head = "\n".join(lines)
    body = "\n".join(["- Latest events (newest first):", *events]) if events else ""
    text = f"{head}\n{body}".strip()
    return text if len(text) <= chars else text[:chars].rsplit("\n", 1)[0] + "\n  …"


def instruction(session: str) -> str:
    """The context a turn of `session` carries, when it is attached to one."""
    if not _enabled():
        return ""
    ws = for_session(session)
    return render(ws) if ws else ""


def on_message(session: str, text: str, by: str = "") -> dict | None:
    """A message in a meeseek conversation: the conversation's workstream —
    the one it is attached to, else the one the message names (attached now)."""
    if not _enabled():
        return None
    ws = for_session(session)
    if ws is None:
        ws = resolve(keys_in(text), title=_title(text))
        if ws is None:
            return None
        ws = attach_session(ws, session)
    return ws


def _title(text: str) -> str:
    first = " ".join((text or "").split())
    return first[:80]


# --- keeping it current ---------------------------------------------------------------

class _Summary(BaseModel):
    goal: str = Field(default="", description="the piece of work's goal, one sentence")
    state: str = Field(default="", description="where it stands now, one or two sentences")
    next: str = Field(default="", description="the next step, one sentence; empty when none")
    decisions: list[str] = Field(default_factory=list, description="decisions made so far, short, newest last")
    questions: list[str] = Field(default_factory=list, description="questions still open, short")
    title: str = Field(default="", description="a short title for the piece of work")


def update(ws_id: str, said: str, answer: str, by: str = "") -> dict | None:
    """Fold one exchange about the workstream into its summary (a fast-model
    call); the exchange is an event either way."""
    from meeseek.config import cfg

    ws = get(ws_id)
    if ws is None:
        return None
    ws = note(ws, f"{_title(said)} → {_title(answer)}", by)
    if not cfg.workstreams.summarize or not (answer or "").strip():
        return ws
    from meeseek.context import prompts
    from meeseek.runtime.oneshot import ask

    try:
        got = ask(prompts.text("internal/workstream_update", current=render(ws, 4000),
                               said=(said or "")[:3000], answer=(answer or "")[:4000]),
                  model=cfg.models.fast, schema=_Summary, temperature=0, timeout_s=60)
    except Exception as e:  # noqa: BLE001 — the event stands without the summary
        logger.warning("[workstreams] summary of %s failed: %s", ws_id, e)
        return ws
    ws = get(ws_id) or ws
    for field in ("goal", "state", "next"):
        if getattr(got, field).strip():
            ws[field] = getattr(got, field).strip()[:400]
    ws["decisions"] = [d.strip()[:200] for d in got.decisions if d.strip()][-MAX_DECISIONS:] or ws["decisions"]
    ws["questions"] = [q.strip()[:200] for q in got.questions if q.strip()][:MAX_QUESTIONS]
    if got.title.strip() and not ws.get("titled"):     # the first message stood in until now
        ws["title"], ws["titled"] = got.title.strip()[:80], True
    return save(ws)


def update_later(ws_id: str, said: str, answer: str, by: str = "") -> None:
    """`update` off the caller's path: a turn's answer is not held up by it."""
    threading.Thread(target=update, args=(ws_id, said, answer, by), daemon=True,
                     name=f"workstream-{ws_id}").start()


def after_turn(session: str, said: str, answer: str, by: str = "") -> None:
    """A meeseek turn ended: fold it into its workstream, when it has one."""
    if not _enabled():
        return
    ws = for_session(session)
    if ws:
        update_later(ws["id"], said, answer, by or "meeseek")


async def on_claude_turn(record: dict) -> None:
    """A Claude session meeseek handed work about a workstream ended: the
    session is the workstream's, and its answer goes into the summary."""
    ws_id = (record.get("origin") or {}).get("workstream", "")
    ws = get(ws_id) if ws_id else None
    if ws is None or not record.get("session_id"):
        return
    attach_claude(ws, record["session_id"], record.get("cwd", ""))
    asked = next((h["text"] for h in reversed(record.get("handoffs", [])) if h["from"] == "meeseek"), "")
    answered = next((h["text"] for h in reversed(record.get("handoffs", [])) if h["from"] == "claude"), "")
    asked = asked.split("\n\n---\n\n", 1)[-1]          # the brief without the context it was led by
    update_later(ws["id"], asked, answered, "claude")


def install() -> None:
    """At server start: hear the Claude sessions meeseek hands work to."""
    from meeseek.app import app

    if on_claude_turn not in app().after_claude_turn:
        app().after_claude_turn.append(on_claude_turn)


# --- outside events -------------------------------------------------------------------

def _group_key(group: str) -> str:
    """An Inbox group as a workstream key: mr:<path>!<iid> stays, jira:<KEY> is the key."""
    if group.startswith("jira:"):
        return group[5:]
    return group if group.startswith("mr:") else ""


def on_event(group: str, text: str, by: str = "", kind: str = "") -> dict | None:
    """News on a ticket or an MR (an Inbox item): an event on its workstream,
    when one exists — news alone does not open one. A merge closes it."""
    if not _enabled():
        return None
    ws = find(_group_key(group))
    if ws is None or ws.get("status") != "open":
        return None
    ws = note(ws, text, by)
    _route(ws, kind, text)
    if kind == "merged":
        ws = close(ws, text)
    return ws


def _route(ws: dict, kind: str, text: str) -> None:
    """An event the person opted into (workstreams.route_to_claude) continues
    the workstream's Claude session."""
    from meeseek.config import cfg

    if kind not in cfg.workstreams.route_to_claude or not ws.get("claude"):
        return
    from meeseek.context import prompts
    from meeseek.runtime import claude_agent

    last = sorted(ws["claude"], key=lambda c: c.get("at", 0))[-1]
    try:
        claude_agent.start(prompts.text("internal/workstream_event", context=render(ws), event=text),
                           cwd=last.get("cwd", ""), session_id=last["session_id"],
                           origin={"workstream": ws["id"]})
    except Exception as e:  # noqa: BLE001 — the event stands on the workstream
        logger.warning("[workstreams] could not tell Claude about %s: %s", ws["id"], e)


def close(ws: dict, why: str = "") -> dict:
    """The work is done: closed, its summary kept in the knowledge base."""
    ws = get(ws["id"]) or ws
    if ws.get("status") == "closed":
        return ws
    ws["status"] = "closed"
    ws["closed"] = {"at": time.time(), "why": why[:200]}
    ws = save(ws)
    try:
        folder = home().notes / "workstreams"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{ws['id']}.md").write_text(render(ws, 8000) + f"\n\nClosed: {why}\n", encoding="utf-8")
    except OSError as e:
        logger.warning("[workstreams] could not save %s's summary: %s", ws["id"], e)
    return ws


# --- for the console, the hook and the plugins ------------------------------------------

def rows(include_closed: bool = False) -> list[dict]:
    """Every workstream, the latest first, as the console lists them."""
    out = []
    for w in _all():
        if w.get("status") != "open" and not include_closed:
            continue
        out.append({"id": w["id"], "key": w["key"], "aliases": w.get("aliases", []), "title": w.get("title", ""),
                    "status": w.get("status", "open"), "state": w.get("state", ""), "next": w.get("next", ""),
                    "updated": w.get("updated_at", 0), "sessions": w.get("sessions", []),
                    "claude": [c["session_id"] for c in w.get("claude", [])],
                    "events": len(w.get("events", []))})
    return sorted(out, key=lambda r: r["updated"], reverse=True)


def lookup(text: str = "", branch: str = "", claude_session: str = "", cwd: str = "") -> dict | None:
    """The workstream a Claude Code prompt is about — the keys it names, or
    its branch (whose name often holds the ticket key) — and, given the Claude
    session, that session attached to it. Opens none."""
    if not _enabled():
        return None
    ws = for_claude(claude_session)
    if ws is None:
        keys = keys_in(text)
        if branch:
            keys = [*keys, *[k for k in keys_in(branch) if k not in keys]]
        ws = resolve(keys, create=False) if keys else None
        if ws is None and branch:
            ws = find(branch_key(branch))
    if ws and claude_session:
        ws = attach_claude(ws, claude_session, cwd, branch)
    return ws


def workstream_get(key: str = "", branch: str = "") -> str:
    """The plugin: a workstream's context, by a ticket key, an MR link or a branch."""
    ws = None
    if key:
        ks = keys_in(key) or [key.strip()]
        ws = next((w for w in (find(k) for k in ks) if w), None)
    if ws is None and branch:
        ws = lookup(branch=branch)
    if ws is None:
        return "No workstream for that yet. One starts when meeseek works on the ticket or MR."
    return render(ws, 6000)


def workstream_note(key: str, text: str, _origin: dict | None = None) -> str:
    """The plugin: note something on a workstream (a decision, a result, what is next)."""
    ks = keys_in(key) or [key.strip()]
    ws = resolve(ks, create=True)
    if ws is None:
        return "Name one ticket key, MR link or branch."
    by = (_origin or {}).get("user") or "claude"
    note(ws, text, by)
    return f"Noted on {ws['key']}."
