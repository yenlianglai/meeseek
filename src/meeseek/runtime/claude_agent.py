"""Claude Code as an agent meeseek hands work to.

meeseek supplies the context and the way in and out; Claude does the work
(code, debugging, pages, documents) on this machine's Claude Code login — the
subscription, no API key. The hand-over is one way: a message and a folder in,
a reply and the files it wrote out. Claude's only meeseek tools are the
stateless plugins (runtime/mcp_server.py:plugin_tools); it writes to no team
system.

What Claude may do (policy()), in auto mode: whatever auto mode allows inside
its folder and the outputs directory, plus the plugins. Writes elsewhere, other
MCP tools, and anything auto mode would ask a person about are refused — no
person is in the loop. No user or project Claude settings are loaded, so
nothing widens this.

Any Claude Code session on this machine can be continued; one someone else
wrote to in the last few minutes may be open in a terminal, so it is forked.
Each driven session gets a record in tasks_dir(); usage goes to usage_log() at
US$0. Turns run on this module's own loop thread, so both an agent turn's
worker thread and the server can start one and wait on it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
import time
import uuid
from pathlib import Path

from meeseek.app import app
from meeseek.config.paths import home
from meeseek.context import prompts

logger = logging.getLogger(__name__)


def tasks_dir() -> Path:
    return home().data / "claude_tasks"


def usage_log() -> Path:
    return home().data / "claude_usage.jsonl"


# Claude Desktop's Code tab keeps its own list of the sessions it opened, each
def scratch_dir() -> Path:
    return home().data / "claude-scratch"


# naming the CLI session (the same ~/.claude/projects transcript) it runs on.
DESKTOP_SESSIONS = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
RECENT_S = 600            # a session someone else wrote this recently may be open: fork it
SOURCE = "claude"         # its name on the Usage page
# One message from Claude Code can carry an image it read: far past the SDK's
# 1 MB default, which ends the turn.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
_FILE_WRITES = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
_FILE_READS = {"Read", "Glob", "Grep", "LS", "NotebookRead"}
# meeseek's MCP tools that are not plugins — an agent turn (ask_meeseek,
# check_meeseek) or a write — kept out of Claude's tool list altogether.
_NOT_PLUGINS = ("ask_meeseek", "check_meeseek", "comment_mr", "comment_mr_lines", "reply_mr_comment",
                "create_mr", "update_mr")
_QUIET = {"Read", "Glob", "Grep", "LS", "NotebookRead", "TodoWrite", "WebSearch", "WebFetch", "Task", "Agent"}
_RAN_KEEP = 200

# Called with the record after each turn — how a phone request gets its answer.

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()
_running: dict[str, asyncio.Task] = {}        # record id → its turn


def loop() -> asyncio.AbstractEventLoop:
    """This module's event loop, started on first use."""
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="claude-agent", daemon=True).start()
    return _loop


def _submit(coro):
    return asyncio.run_coroutine_threadsafe(coro, loop())


# --- what Claude may do --------------------------------------------------------

def _inside(root: Path, path: str) -> bool:
    if not path:
        return True   # Glob / Grep without a path search the working directory
    p = Path(path).expanduser()   # a shell expands ~ before the command sees it
    p = (p if p.is_absolute() else root / p).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _yours(root: Path, path: str) -> bool:
    """Inside the folder Claude was given — or the outputs directory, where
    the pages and pictures it makes for people go."""
    from meeseek.config.paths import home

    return _inside(root, path) or (bool(path) and _inside(home().outputs, path))


def plugins() -> set[str]:
    """meeseek's plugins as Claude sees them (mcp__meeseek__<name>)."""
    from meeseek.runtime.mcp_server import plugin_tools

    return {f"mcp__meeseek__{n}" for n in plugin_tools()}


def policy(root: Path, tool: str, args: dict, read_only: bool = False) -> str:
    """Why `tool` with `args` is refused; empty when auto mode may decide it.
    A read-only task (start(read_only=True)) writes no file at all."""
    if tool.startswith("mcp__"):
        if tool in plugins():
            return ""
        return (f"{tool} is not available here. meeseek's plugins are: looking up context, drawing "
                "and review_page; writes to the team's systems are meeseek's to make, not yours.")
    if tool in _FILE_WRITES and read_only:
        return "This task is read-only: change no file. Say in your final message what you would change."
    if tool in _FILE_WRITES:
        path = args.get("file_path") or args.get("notebook_path") or args.get("path") or ""
        if not _yours(root, path):
            return f"{path} is outside your folder and the outputs directory; write only there."
    return ""


def _describe(tool: str, args: dict) -> str:
    for key in ("file_path", "path", "pattern", "command", "url", "query", "prompt"):
        if args.get(key):
            return f"{tool} {str(args[key])[:160]}"
    return tool


def _note_ran(record: dict, tool: str, args: dict) -> None:
    """What Claude ran on its own — kept on the record, for whoever asks later."""
    if tool in _QUIET:
        return
    record.setdefault("ran", []).append({"tool": tool, "input": _describe(tool, args), "at": time.time()})
    record["ran"] = record["ran"][-_RAN_KEEP:]
    save(record)


# --- the records ---------------------------------------------------------------

def _path(record_id: str) -> Path:
    return tasks_dir() / f"{record_id}.json"


def save(record: dict) -> None:
    tasks_dir().mkdir(parents=True, exist_ok=True)
    record["updated_at"] = time.time()
    _path(record["id"]).write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")


def records() -> list[dict]:
    if not tasks_dir().exists():
        return []
    out = []
    for p in tasks_dir().glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def record_of(session_id: str) -> dict | None:
    return next((r for r in records() if r.get("session_id") == session_id), None)


def get(record_id: str) -> dict | None:
    p = _path(record_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _record_usage(model: str, usage: dict, estimate: float | None) -> None:
    """Calls and tokens for the Usage page; the API-price estimate Claude Code
    reports is kept for reference — the subscription is not billed per call."""
    usage = usage or {}
    row = {"ts": time.time(), "model": model, "source": SOURCE,
           "prompt": int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
           + int(usage.get("cache_creation_input_tokens") or 0),
           "cached": int(usage.get("cache_read_input_tokens") or 0),
           "output": int(usage.get("output_tokens") or 0), "api_estimate_usd": float(estimate or 0.0)}
    usage_log().parent.mkdir(parents=True, exist_ok=True)
    with usage_log().open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


# --- the sessions --------------------------------------------------------------

def desktop_sessions() -> dict[str, dict]:
    """The sessions Claude Desktop opened, by CLI session id: its title, and
    whether it is archived. Desktop's own files — read only, and a file it is
    halfway through writing is skipped."""
    out: dict[str, dict] = {}
    if not DESKTOP_SESSIONS.is_dir():
        return out
    for p in DESKTOP_SESSIONS.glob("*/*/local_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("cliSessionId"):
            out[d["cliSessionId"]] = {"title": d.get("title") or "", "archived": bool(d.get("isArchived"))}
    return out


def sessions(query: str = "", limit: int = 30) -> list[dict]:
    """Claude Code sessions on this machine, newest first, with what meeseek knows of each."""
    from claude_agent_sdk import list_sessions

    mine = {r["session_id"]: r for r in records() if r.get("session_id")}
    desktop = desktop_sessions()
    q = query.strip().lower()
    out = []
    for s in list_sessions(limit=max(limit * 4, 200)):
        d = desktop.get(s.session_id)
        title = (d and d["title"]) or s.custom_title or s.summary or (s.first_prompt or "")[:80]
        if q and not any(q in (x or "").lower() for x in (title, s.first_prompt, s.cwd, s.git_branch, s.session_id)):
            continue
        r = mine.get(s.session_id)
        out.append({
            "session_id": s.session_id, "title": title, "first_prompt": (s.first_prompt or "")[:300],
            "cwd": s.cwd or "", "branch": s.git_branch or "",
            "updated_at": int(s.last_modified or 0) / 1000, "created_at": int(s.created_at or 0) / 1000,
            "by_meeseek": r is not None, "record": r["id"] if r else "",
            "desktop": d is not None, "archived": bool(d and d["archived"]),
            "status": r["status"] if r else "",
        })
        if len(out) >= limit:
            break
    return out


def session_info(session_id: str) -> dict | None:
    from claude_agent_sdk import get_session_info

    s = get_session_info(session_id)
    if s is None:
        return None
    return {"session_id": s.session_id, "cwd": s.cwd or "", "branch": s.git_branch or "",
            "title": s.custom_title or s.summary or (s.first_prompt or "")[:80],
            "updated_at": int(s.last_modified or 0) / 1000}


# --- a turn --------------------------------------------------------------------

def _about_you() -> str:
    """What meeseek knows about the person (context/personal.py), so Claude works
    the way they want too; empty when nothing is known or it cannot be read."""
    try:
        from meeseek.context import personal

        return personal.summary()
    except Exception:  # noqa: BLE001 — a broken personal store never stops a hand-over
        return ""


def _notes(cwd: Path) -> str:
    for name in ("CLAUDE.md", "AGENTS.md"):
        p = cwd / name
        if p.is_file():
            return prompts.text("agents/claude_agent#notes", file=name,
                                text=p.read_text(encoding="utf-8", errors="replace")[:12000])
    return ""


def _options(record: dict, resume: str, fork: bool):
    from claude_agent_sdk import (ClaudeAgentOptions, HookMatcher, PermissionResultAllow,
                                  PermissionResultDeny)

    from meeseek.config import cfg

    root = Path(record["cwd"])
    read_only = bool(record.get("read_only"))

    async def gate(input_data, tool_use_id, context):
        tool, args = input_data.get("tool_name", ""), input_data.get("tool_input") or {}
        reason = policy(root, tool, args, read_only)
        if reason:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}}
        _note_ran(record, tool, args)
        return {}

    async def can_use_tool(tool, args, context):
        # Reached when auto mode stops to ask. What meeseek allows outright — its
        # plugins, files in the folder or outputs — goes ahead; anything else
        # would need a person, and none is in the loop.
        path = args.get("file_path") or args.get("notebook_path") or args.get("path") or ""
        ours = tool in plugins() or (tool in _FILE_WRITES | _FILE_READS and _yours(root, path))
        if ours and not policy(root, tool, args, read_only):
            return PermissionResultAllow(updated_input=args)
        return PermissionResultDeny(message=policy(root, tool, args, read_only) or (
            "This needs a person's go-ahead and none is in the loop. Do not do it; say in your final "
            "message what you would run and why, and the person can run it."))

    mcp = {"meeseek": {"type": "http", "url": cfg.claude.meeseek_mcp_url}} if cfg.claude.meeseek_mcp_url else {}
    append = prompts.text("agents/claude_agent#system", notes=_notes(root), pages_dir=str(home().outputs / "pages"))
    me = _about_you()
    if me:
        append += "\n\n" + prompts.text("agents/claude_agent#me", me=me)
    return ClaudeAgentOptions(
        cwd=str(root), resume=resume or None, fork_session=fork, setting_sources=[],
        system_prompt={"type": "preset", "preset": "claude_code", "append": append},
        mcp_servers=mcp, disallowed_tools=[f"mcp__meeseek__{n}" for n in _NOT_PLUGINS],
        permission_mode="auto", can_use_tool=can_use_tool,
        hooks={"PreToolUse": [HookMatcher(matcher="*", hooks=[gate])]},
        model=cfg.claude.model or None, max_turns=cfg.claude.max_turns or None,
        max_buffer_size=MAX_MESSAGE_BYTES,
        # The claude.ai account's connectors (Gmail, Calendar, Docs…) would ride
        # into the session — writes to the outside that bypass meeseek. Off.
        env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
    )


async def _turn(record: dict, prompt: str, resume: str, fork: bool) -> None:
    from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, SystemMessage

    record["handoffs"].append({"at": time.time(), "from": "meeseek", "to": "claude", "text": prompt})
    record["status"] = "running"
    save(record)
    reply, result, model, error = "", None, "", ""
    started = time.monotonic()
    try:
        async with ClaudeSDKClient(options=_options(record, resume, fork)) as client:
            await client.query(prompt)
            async for m in client.receive_response():
                if isinstance(m, SystemMessage) and m.subtype == "init":
                    model = (m.data or {}).get("model", "")
                    sid = (m.data or {}).get("session_id", "")
                    if sid and sid != record.get("session_id"):
                        record["session_id"] = sid
                        save(record)
                elif isinstance(m, AssistantMessage):
                    text = "".join(getattr(b, "text", "") for b in m.content)
                    if text.strip():
                        reply = text
                elif isinstance(m, ResultMessage):
                    result = m
    except Exception as e:  # noqa: BLE001 — a failed turn is a record state
        logger.exception("[claude] turn on %s failed", record["id"])
        error = f"{type(e).__name__}: {e}"
    if result is not None:
        record["session_id"] = result.session_id or record.get("session_id", "")
        reply = result.result or reply
        _record_usage(model, result.usage or {}, result.total_cost_usd)
        if result.is_error:
            error = error or result.subtype
    elif not error:
        error = "no result"
    record["handoffs"].append({
        "at": time.time(), "from": "claude", "to": "meeseek", "text": reply or "(no reply)",
        "session_id": record.get("session_id", ""), "model": model,
        "turns": result.num_turns if result else 0,
        "seconds": round(time.monotonic() - started), "error": error,
    })
    record["status"] = "failed" if error else "done"
    save(record)


async def _task(record: dict, prompt: str, resume: str, fork: bool) -> None:
    """A message to Claude, then the hooks that relay the outcome."""
    await _turn(record, prompt, resume, fork)
    _wake(record["id"])
    for fn in list(app().after_claude_turn):
        try:
            await fn(record)
        except Exception:  # noqa: BLE001
            logger.exception("[claude] after-turn hook failed")


def start(prompt: str, *, cwd: str = "", session_id: str = "", origin: dict | None = None,
          awaited_for: float = 0, read_only: bool = False) -> dict:
    """Start a new session, or continue `session_id`, in the background; the
    record (its id is how to wait on it). `read_only`: Claude may write no file."""
    from meeseek.config import cfg

    if not cfg.claude.enabled:
        raise ValueError("Claude Code is turned off (claude.enabled in project.yaml)")
    record = record_of(session_id) if session_id else None
    fork = False
    info = None
    if session_id:
        info = session_info(session_id)
        if info is None:
            raise ValueError(f"no Claude session {session_id}")
        cwd = info["cwd"] or cwd
        if record and running(record["id"]):
            raise ValueError("this session is still working; wait for it first")
        # Someone else wrote to it a moment ago — maybe a terminal still has it open.
        mine_last = record["updated_at"] if record else 0
        fork = time.time() - info["updated_at"] < RECENT_S and info["updated_at"] > mine_last + 5
    if not cwd:   # nowhere in particular: a scratch folder of its own
        cwd = str(scratch_dir() / uuid.uuid4().hex[:8])
        Path(cwd).mkdir(parents=True, exist_ok=True)
    folder = Path(cwd).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"no such folder: {cwd}")
    if record is None or fork:
        title = info["title"] if info else (prompt.strip().splitlines() or ["Claude"])[0]
        record = {
            "id": uuid.uuid4().hex[:10], "session_id": "" if fork else session_id,
            "forked_from": session_id if fork else "", "cwd": str(folder), "title": title[:80],
            "origin": origin or {}, "status": "running", "handoffs": [], "created_at": time.time(),
        }
    # Whoever waits this long relays the answer itself; past it, it is delivered.
    record["await_until"] = time.time() + awaited_for
    record["origin"] = origin or record.get("origin") or {}
    record["read_only"] = read_only
    save(record)
    _submit(_launch(record, prompt, session_id, fork)).result(timeout=10)
    return record


def running(record_id: str) -> bool:
    task = _running.get(record_id)
    return task is not None and not task.done()


SCRATCH_KEEP_DAYS = 7   # a scratch folder outlives its last run this long, to carry the session on


def prune_scratch(now: float | None = None) -> int:
    """Remove the scratch folders (a task with nowhere in particular to work)
    not used for SCRATCH_KEEP_DAYS and not running; how many."""
    root = scratch_dir()
    if not root.is_dir():
        return 0
    cutoff = (now or time.time()) - SCRATCH_KEEP_DAYS * 86400
    last: dict[str, float] = {}
    busy: set[str] = set()
    for r in records():
        cwd = str(Path(r.get("cwd", "")).resolve())
        last[cwd] = max(last.get(cwd, 0.0), r.get("updated_at") or r.get("created_at") or 0.0)
        if running(r.get("id", "")):
            busy.add(cwd)
    n = 0
    for folder in root.iterdir():
        key = str(folder.resolve())
        if folder.is_dir() and key not in busy and max(last.get(key, 0.0), folder.stat().st_mtime) < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            n += 1
    return n


async def _launch(record: dict, prompt: str, resume: str, fork: bool) -> None:
    _running[record["id"]] = asyncio.get_running_loop().create_task(_task(record, prompt, resume, fork))


def wait(record_id: str, timeout: float) -> dict:
    """Block until the record's turn ends or `timeout` passes; the record."""
    task = _running.get(record_id)
    if task is not None:
        async def done():
            await asyncio.wait({task}, timeout=timeout)
        _submit(done()).result(timeout=timeout + 5)
    return get(record_id) or {}


def _wake(record_id: str) -> None:
    """A turn on this record ended: the wake waiting on it (tools/claude.py
    filed one when nobody was left waiting) comes due now."""
    try:
        from meeseek.runtime import wakes

        wakes.on_event(f"claude:{record_id}")
    except Exception:  # noqa: BLE001 — the wake still fires at its timeout
        logger.debug("could not fire the wake for %s", record_id, exc_info=True)


def recover() -> int:
    """After a restart: a turn that was running has stopped. The wake waiting
    on it, if any, comes due, so the asking session hears about the break."""
    n = 0
    for r in records():
        if r["status"] in ("running", "waiting"):
            r["status"] = "failed"
            r["handoffs"].append({"at": time.time(), "from": "claude", "to": "meeseek",
                                  "text": "(interrupted by a server restart)", "error": "interrupted"})
            save(r)
            _wake(r["id"])
            n += 1
    return n


def directive(session_id: str, title: str = "") -> str:
    """The line a chat puts before a message to say which Claude session the
    task goes to ("new" = a new one) — the orchestrator's prompt reads it."""
    tail = f" · {title}" if title and session_id != "new" else ""
    return f"[Claude session: {session_id}{tail}]"


def last_reply(record: dict) -> str:
    replies = [h for h in record.get("handoffs", []) if h["from"] == "claude"]
    return replies[-1]["text"] if replies else ""
