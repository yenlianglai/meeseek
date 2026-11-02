"""meeseek as an MCP server — the mirror of adapters/mcp.py.

Makes meeseek callable by MCP hosts (Claude Code, Claude Desktop, …) under the
server name `meeseek`. The surface, split by cost:

    ask_meeseek          — a request to meeseek's agents; waits up to
                           wait_seconds, then hands back a task_id (0 = hand it
                           back at once). No request + a session_id carries on
                           a turn that stopped part-way.
    check_meeseek        — a task's progress so far, then its result; or stop it
    plugins              — kb_grep, jira_search, get_mr, generate_image,
                           review_page, …: one stateless call each, no agent turn
    MR writes            — update_mr, create_mr, comment_mr, comment_mr_lines,
                           reply_mr_comment
    list_outputs, outputs://… — what meeseek produced, and reading it back

Results carry a `status`, and `needs_input` lists the team's questions so the
host can put them to its user instead of parsing prose. Sessions are keyed by
(caller, session_id) — two hosts never share history — and turns on one
session are serialized.

Mounted on the running FastAPI server rather than stdio, so a call reuses the
warm process (skills loaded, MCP servers connected, the event store open).
Conversations land under `mcp` / `mcp:<caller>` users: out of the console's
session list, but visible in Trace and counted by the usage dashboard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import TypedDict

from meeseek.app import app
from meeseek.context import prompts
from meeseek.runtime.images import ImageError

logger = logging.getLogger(__name__)

MCP_USER = "mcp"
_TURN_LIMIT = 60  # model calls for one request; a routed turn fans out to tools
_MAX_BACKGROUND_TASKS = 4
_PROGRESS_LINES = 15  # most recent progress entries returned while running
# How long ask_meeseek blocks before handing back a task_id instead. LLM
# hosts rarely override the default, and a routed turn can run for many
# minutes, so the default is bounded: the common request answers in one round
# trip and a long one degrades to a background task the caller can poll.
_DEFAULT_WAIT_SECONDS = 90
_OUTPUT_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
_MAX_OUTPUT_FILE_BYTES = 8 * 1024 * 1024


class Result(TypedDict):
    """What ask_meeseek and check_meeseek return.

    status: answered | needs_input | running | interrupted | failed | unknown
    needs_input: meeseek's clarifying questions, one per entry, when status is
        needs_input — answer them with ask_meeseek on the same session_id.
    task_id: the turn's handle; check_meeseek takes it while status is running.
    progress: while running, the most recent tool calls and text, oldest first.
    """

    status: str
    answer: str
    session_id: str
    handled_by: str
    needs_input: list[str]
    task_id: str
    progress: list[str]
    tool_calls: int
    elapsed_seconds: int


def _result(status: str, answer: str = "", **fields) -> Result:
    base = dict(session_id="", handled_by="", needs_input=[], task_id="", progress=[],
                tool_calls=0, elapsed_seconds=0)
    return Result(status=status, answer=answer, **{**base, **fields})


# task_id -> {"task": asyncio.Task, "session_id": str, "progress": dict}
# (user_id, session_id) -> lock; a session runs one turn at a time

def _describe_team() -> str:
    """Build the tool description from the skills actually installed."""
    from meeseek.runtime.skills import discover_skills

    try:
        lines = [f"  - {s.name}: {s.description.strip().splitlines()[0]}"
                 for s in discover_skills() if s.description.strip()]
    except Exception:  # a broken skill must not stop the server from starting
        logger.warning("could not enumerate skills for the MCP tool description",
                       exc_info=True)
        lines = []
    return "\n".join(lines)


def _user_for(caller: str) -> str:
    """Sessions are scoped per caller so two hosts never share a history."""
    caller = re.sub(r"[^A-Za-z0-9_.:-]+", "-", (caller or "").strip())[:64].strip("-")
    return f"{MCP_USER}:{caller}" if caller else MCP_USER


def _session_lock(user_id: str, session_id: str) -> asyncio.Lock:
    return app().mcp_session_locks.setdefault((user_id, session_id), asyncio.Lock())


def _new_progress() -> dict:
    return {"lines": [], "tool_calls": 0, "last_text": "", "started": time.time(),
            "pending": {}}


def _summarize_args(args) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        s = str(args)
    return s if len(s) <= 80 else s[:77] + "…"


def _note_core_event(progress: dict | None, e) -> None:
    """Fold one of the engine's events into the task's progress record."""
    from meeseek.core.events import ModelStepped, Replied, ToolHeld, ToolRan

    if progress is None:
        return
    now = time.time()
    if isinstance(e, ModelStepped):
        for c in e.calls:
            progress["tool_calls"] += 1
            progress["pending"][c.id or c.name] = (c.name, now)
            progress["lines"].append(f"+{now - progress['started']:.0f}s {e.by} → {c.name}({_summarize_args(c.args)})")
    elif isinstance(e, (ToolRan, ToolHeld)):
        started = progress["pending"].pop(e.call.id or e.call.name, (e.call.name, now))[1]
        mark = " ⏸" if isinstance(e, ToolHeld) else (" ✗" if e.failed else "")
        progress["lines"].append(f"+{now - progress['started']:.0f}s   {e.call.name} done {now - started:.1f}s{mark}")
    elif isinstance(e, Replied):
        progress["last_text"] = e.text[-600:]
    progress["invocation_id"] = e.turn
    if len(progress["lines"]) > 200:
        del progress["lines"][:-200]


async def _run_request(request: str, session_id: str, images=None, *, user_id: str = MCP_USER,
                       progress: dict | None = None, resume: str = "") -> tuple[str, str, list[str]]:
    """One orchestrator turn: (final text, handling agent, open questions).

    `resume` is a stopped turn's id: that turn carries on from its last step
    (engine.resume) instead of a new one starting. `images` are (mime, bytes) pairs already read and
    validated server-side — see runtime/images.py for why the caller never
    sends the bytes.
    """
    from meeseek import engine
    from meeseek.core.events import open_question
    from meeseek.runtime.inbox import INBOX_USER

    watched = not (user_id == INBOX_USER or user_id.startswith("mcp"))
    observe = lambda e: _note_core_event(progress, e)  # noqa: E731
    if resume and not request:
        res = await engine.resume(session_id, by=user_id, watched=watched, observe=observe)
    else:
        res = await engine.turn(session_id, request, by=user_id, watched=watched, observe=observe,
                                attachments=images or ())
    question = open_question(engine.store().read(session_id))
    needs = []
    if question is not None:
        from meeseek.runtime.ask import Question

        a = question.args
        needs = [Question(call_id=question.id, invocation_id=res.turn, question=str(a.get("question", "")),
                          options=[str(o) for o in a.get("options") or []],
                          recommended=str(a.get("recommended") or "")).as_text()]
    final = res.reply or ("meeseek ended the turn without a reply. Ask once more, or check the turn "
                          "in the console's Trace.")
    by = next((e.by for e in reversed(res.events) if e.kind == "replied"), "orchestrator")
    return final, by, needs


async def _run_locked(request, sid, images, *, user_id, progress, resume=""):
    """Serialize turns on one session: a second call waits for the first."""
    async with _session_lock(user_id, sid):
        return await _run_request(request, sid, images, user_id=user_id, progress=progress,
                                  resume=resume)


async def run_orchestrator_turn(request: str, session_id: str, *, user_id: str):
    """One orchestrator turn on a session, queued behind any turn already on
    it: (final text, handling agent, open questions)."""
    return await _run_locked(request, session_id, None, user_id=user_id, progress=None)


async def _collect(paths: list[str] | None, use_clipboard: bool) -> list[tuple[str, bytes]]:
    """Read the request's images off a worker thread — both sources block."""
    from meeseek.runtime.images import collect_images

    if not paths and not use_clipboard:
        return []
    return await asyncio.to_thread(collect_images, paths, use_clipboard)


async def _final_reply(rec: dict, invocation_id: str) -> tuple[str, str]:
    """(text, author) of a finished turn's last reply, read from its session."""
    from meeseek.runtime import history

    for r in reversed(history.rows(session=rec["session_id"], users=(rec["user_id"],))):
        if r.turn != invocation_id or r.event.get("partial"):
            continue
        text = history.text_of(r.event).strip()
        if text:
            return text, r.event.get("author") or "orchestrator"
    return "", "orchestrator"


def _reap_finished_tasks() -> None:
    """Drop completed task records once they have been checked."""
    for tid in [t for t, rec in app().mcp_tasks.items() if rec.get("consumed")]:
        app().mcp_tasks.pop(tid, None)


def _progress_view(rec: dict) -> tuple[list[str], int, int]:
    p = rec.get("progress") or _new_progress()
    lines = list(p["lines"][-_PROGRESS_LINES:])
    if p.get("last_text"):
        lines.append("… " + p["last_text"].replace("\n", " ")[-300:])
    return lines, p["tool_calls"], int(time.time() - p["started"])


def _finish(rec: dict, task_id: str) -> Result:
    rec["consumed"] = True
    sid = rec["session_id"]
    lines, calls, elapsed = _progress_view(rec)
    run = dict(session_id=sid, task_id=task_id, progress=lines, tool_calls=calls,
               elapsed_seconds=elapsed)
    try:
        answer, handler, needs = rec["task"].result()
    except Exception as e:
        logger.exception("[mcp] background task failed")
        from meeseek.runtime.llm import failure_message
        return _result("failed", f"meeseek failed to handle that: {failure_message(e)}", **run)
    return _result("needs_input" if needs else "answered", answer, handled_by=handler,
                   needs_input=needs, **run)


def _start_task(request: str, sid: str, images, *, user_id: str,
                task_id: str = "", resume: str = "") -> str:
    task_id = task_id or f"task-{uuid.uuid4().hex[:12]}"
    progress = _new_progress()
    if resume:
        progress["invocation_id"] = resume
    app().mcp_tasks[task_id] = {
        "task": asyncio.create_task(
            _run_locked(request, sid, images, user_id=user_id, progress=progress, resume=resume)),
        "session_id": sid,
        "user_id": user_id,
        "progress": progress,
    }
    _remember_task(task_id, user_id=user_id, session_id=sid, started=progress["started"],
                   invocation_id=resume)
    return task_id


# Task records outlive the process: a restart (a deploy, a crash) leaves the
# turn in the session, and these records find it again.
def _task_store():
    from meeseek.config.paths import home
    from meeseek.runtime.store import JsonlStore

    return JsonlStore(home().data / "mcp_tasks.jsonl")


_MAX_TASK_RECORDS = 500


def _remember_task(task_id: str, **record) -> None:
    def keep(records):
        rest = [r for r in records if r.get("task_id") != task_id]
        return (rest + [{"task_id": task_id, **record}])[-_MAX_TASK_RECORDS:]
    try:
        _task_store().update(keep)
    except Exception:
        logger.warning("[mcp] could not record task %s", task_id, exc_info=True)


async def _stored_turn(task_id: str) -> tuple[dict, str, str] | None:
    """(record, invocation_id, status) of a task no longer in memory."""
    from meeseek.runtime import history

    rec = next((r for r in _task_store().read() if r.get("task_id") == task_id), None)
    if rec is None:
        return None
    found = history.rows(session=rec["session_id"], users=(rec["user_id"],))
    # The turn the task started: the first one at or after its start.
    inv = rec.get("invocation_id") or next((r.turn for r in found if r.turn and r.at >= rec["started"] - 1), "")
    return rec, inv, (history.turn_status(found, inv) if inv else "unknown")


# --- outputs: what the team produced --------------------------------------

def _outputs_dir() -> Path:
    from meeseek.config.paths import home

    return home().outputs


def _resolve_output(path: str) -> Path:
    root = _outputs_dir().resolve()
    target = (root / path.lstrip("/")).resolve()
    if not target.is_relative_to(root):
        raise ValueError("path escapes the outputs directory")
    if not target.is_file():
        raise FileNotFoundError(path)
    if target.stat().st_size > _MAX_OUTPUT_FILE_BYTES:
        raise ValueError(f"{path} is larger than {_MAX_OUTPUT_FILE_BYTES // (1024 * 1024)} MB")
    return target


def list_output_files(subdir: str = "", limit: int = 40) -> list[dict]:
    """Newest first: {path, bytes, modified, uri}."""
    root = _outputs_dir()
    base = (root / subdir.lstrip("/")).resolve() if subdir else root.resolve()
    if not base.is_relative_to(root.resolve()) or not base.exists():
        return []
    files = [p for p in base.rglob("*") if p.is_file() and not p.name.startswith(".")
             and 2 <= len(p.relative_to(root).parts) <= 3]  # resources cover these depths
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:limit]:
        rel = p.relative_to(root).as_posix()
        kind = "image" if p.suffix.lower() in _OUTPUT_IMAGE_MIME else "file"
        out.append({
            "path": rel,
            "bytes": p.stat().st_size,
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
            "uri": f"outputs://{kind}/{rel}",
        })
    return out


def build_mcp_server():
    """The MCP server.

    Returns the FastMCP instance rather than a bare ASGI app: mounting a
    sub-app does not run its lifespan, so the host has to start the session
    manager itself or every call fails with "Task group is not initialized".
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        name="meeseek",
        instructions=prompts.text("runtime/mcp_server#instructions"),
        stateless_http=True,  # each call is its own HTTP exchange
    )

    # Built before registration: the decorator captures the description then,
    # so mutating __doc__ afterwards would never reach the client.
    team = _describe_team()
    # Shared by both request-taking tools: the host has to be told that images
    # go by reference, or it will try to inline base64 it cannot produce.
    image_args = (
        "    image_paths: Paths to image files on the machine running this server\n"
        "        (PNG/JPEG/WebP/HEIC) for the team to look at. Pass paths, never\n"
        "        image bytes — the server reads the files itself.\n"
        "    use_clipboard_image: True to attach the image currently on that\n"
        "        machine's clipboard. This is how a screenshot with no file gets\n"
        "        in: the user copies it, you set this flag. macOS only.\n"
        "    caller: A stable name for who is calling (e.g. the host + project);\n"
        "        sessions are scoped per caller, so pass the same value to\n"
        "        continue a conversation. Omit for a shared default scope."
    )
    description = (
        "Ask meeseek — your agents — to handle a request, end to end.\n\n"
        "The request is routed to the right specialist automatically. Hand over "
        "what needs your systems (Jira, GitLab, Drive, GKE, knowledge "
        "base); do local text edits yourself. For a single lookup, a picture or "
        "a check of a page you built, call the plugin directly instead.\n\n"
        "This includes WRITING, not just reading. The common MR writes have their "
        "own tools here (update_mr, create_mr, comment_mr, comment_mr_lines, "
        "reply_mr_comment) — use "
        "those directly, they skip a routing turn. Everything else the team can "
        "change goes through THIS tool in plain language: Jira tickets, comments, "
        "transitions and sprint moves (the team files them in its ticket format), "
        "Confluence pages, anything spanning several systems. A job with no "
        "matching tool in your list is a job for this one, not a missing "
        "capability — do not report back that you lack permission without asking "
        "first. If a write is actually refused, the answer says why (a token "
        "scope, a repo allowlist, a missing branch), which is a different thing "
        "from having no tool.\n\n"
        "The result is structured: status `answered` (answer is the result), "
        "`needs_input` (meeseek asks — relay `needs_input` to your user and call "
        "again with the same session_id and their answer), `running` (the wait "
        "ran out — check_meeseek with `task_id`), `interrupted` (it stopped "
        "part-way — call again with that session_id and no request to carry on "
        "from where it stopped), or `failed`.\n\n"
        + (f"Specialists available:\n{team}\n\n" if team else "")
        + "Args:\n"
        "    request: What you want done, in plain language (Chinese or English).\n"
        "        Leave it empty, with a session_id, to carry on an interrupted turn.\n"
        "    session_id: Pass the id returned by a previous call to continue that\n"
        "        conversation; omit to start a fresh one.\n"
        f"    wait_seconds: How long to wait for the answer. Default "
        f"{_DEFAULT_WAIT_SECONDS}s;\n"
        "        past that you get status `running` with a task_id and the work\n"
        "        keeps going in the background — check_meeseek it, or get on with\n"
        "        something else and check back. 0 hands the task_id back at once,\n"
        "        for work you know takes minutes (a whole-MR review).\n"
        + image_args
    )

    def _running() -> int:
        return sum(1 for rec in app().mcp_tasks.values() if not rec["task"].done())

    async def _await(task_id: str, wait: float) -> Result:
        """The task's result if it ends within `wait` seconds, else where it is."""
        rec = app().mcp_tasks[task_id]
        if wait and wait > 0:
            try:
                await asyncio.wait_for(asyncio.shield(rec["task"]), timeout=wait)
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass  # surfaced by _finish below
        if rec["task"].done():
            return _finish(rec, task_id)
        lines, calls, elapsed = _progress_view(rec)
        return _result(
            "running",
            f"Still working after {elapsed}s ({calls} tool calls so far). "
            f"check_meeseek('{task_id}') for progress and the result.\n" + "\n".join(lines),
            session_id=rec["session_id"], task_id=task_id, progress=lines,
            tool_calls=calls, elapsed_seconds=elapsed)

    async def _status_from_session(task_id: str) -> Result:
        found = await _stored_turn(task_id)
        if found is None:
            return _result("unknown", "No such task.", task_id=task_id)
        rec, inv, status = found
        base = dict(task_id=task_id, session_id=rec["session_id"],
                    elapsed_seconds=int(time.time() - rec["started"]))
        if status in ("done", "waiting"):
            from meeseek.runtime import history

            answer, handler = await _final_reply(rec, inv)
            question = history.pending_question(history.rows(session=rec["session_id"], users=(rec["user_id"],)))
            needs = [question.as_text()] if question else []
            return _result("needs_input" if needs else "answered", answer, handled_by=handler,
                           needs_input=needs, **base)
        if status == "interrupted":
            return _result("interrupted",
                           "The task stopped part-way (stopped, or the server restarted). "
                           f"ask_meeseek(session_id='{rec['session_id']}') with no request "
                           "carries it on from where it stopped.", **base)
        return _result("unknown", "The task left no turn in its session.", **base)

    async def _carry_on(user_id: str, sid: str, wait: float) -> Result:
        """Resume the session's last turn from its last step, if it stopped part-way."""
        rec = next((r for r in reversed(_task_store().read())
                    if r.get("session_id") == sid and r.get("user_id") == user_id), None)
        if rec is None:
            return _result("unknown", "meeseek has no turn on this session to carry on.",
                           session_id=sid)
        task_id = rec["task_id"]
        live = app().mcp_tasks.get(task_id)
        if live is not None and not live["task"].done():
            return await _await(task_id, wait)
        found = await _stored_turn(task_id)
        if found is None or found[2] != "interrupted":
            return await _status_from_session(task_id)
        _, inv, _ = found
        app().mcp_tasks.pop(task_id, None)
        _start_task("", sid, None, user_id=user_id, task_id=task_id, resume=inv)
        return await _await(task_id, wait)

    @mcp.tool(description=description)
    async def ask_meeseek(
        request: str = "",
        session_id: str = "",
        wait_seconds: int = _DEFAULT_WAIT_SECONDS,
        image_paths: list[str] | None = None,
        use_clipboard_image: bool = False,
        caller: str = "",
    ) -> Result:
        user_id = _user_for(caller)
        _reap_finished_tasks()
        if not request.strip():
            if not session_id:
                return _result("failed", "Say what you want done (request), or pass the "
                                         "session_id of an interrupted turn to carry it on.")
            return await _carry_on(user_id, session_id, wait_seconds)
        sid = session_id or f"mcp-{uuid.uuid4().hex[:12]}"
        if wait_seconds <= 0 and _running() >= _MAX_BACKGROUND_TASKS:
            return _result("failed", f"Too many background tasks already running "
                                     f"({_running()}/{_MAX_BACKGROUND_TASKS}). Check or wait "
                                     "for one to finish first.", session_id=sid)
        # Read the images before the turn is queued: the clipboard is live state,
        # and by the time a queued turn ran it could hold something else entirely.
        try:
            images = await _collect(image_paths, use_clipboard_image)
        except ImageError as e:
            # A rejected image is the answer: running the turn without the
            # picture would answer a question the caller did not ask.
            return _result("failed", f"No image was attached, so nothing was started: {e}",
                           session_id=sid)
        task_id = _start_task(request, sid, images, user_id=user_id)
        return await _await(task_id, wait_seconds)

    @mcp.tool(
        description=(
            "Check a task ask_meeseek handed back as `running`. While it runs, "
            "`progress` lists the most recent tool calls and the last text meeseek "
            "wrote, so you can see what it is doing. Final statuses are those of "
            "ask_meeseek: answered, needs_input, interrupted, failed.\n\n"
            "Args:\n"
            "    task_id: From ask_meeseek.\n"
            "    wait_seconds: Wait up to this long for it to finish (default 0:\n"
            "        answer at once).\n"
            "    stop: True to stop the task. What it did so far is kept, and\n"
            "        ask_meeseek(session_id=…) with no request carries it on later."
        )
    )
    async def check_meeseek(task_id: str, wait_seconds: int = 0, stop: bool = False) -> Result:
        rec = app().mcp_tasks.get(task_id)
        if stop:
            if rec is not None and not rec["task"].done():
                rec["task"].cancel()
                try:
                    await rec["task"]
                except BaseException:
                    pass
                app().mcp_tasks.pop(task_id, None)
            return await _status_from_session(task_id)
        if rec is None:
            return await _status_from_session(task_id)
        return await _await(task_id, wait_seconds)

    # --- Outputs: files the team produced --------------------------------------

    @mcp.tool(
        description=(
            "List files meeseek produced (reports, reviews, diagrams, "
            "generated images, daily plans) under knowledge_base/outputs, newest "
            "first. Each entry has a `uri` you can read as an MCP resource "
            "(outputs://file/<path> for text, outputs://image/<path> for images).\n\n"
            "Args:\n"
            "    subdir: Restrict to one category, e.g. 'reports', 'diagrams', 'images'.\n"
            "    limit: Max entries (default 40)."
        )
    )
    async def list_outputs(subdir: str = "", limit: int = 40) -> list[dict]:
        return await asyncio.to_thread(list_output_files, subdir, max(1, min(int(limit), 200)))

    @mcp.resource("outputs://list", mime_type="application/json",
                  description="Files meeseek produced, newest first.")
    def outputs_index() -> str:
        return json.dumps(list_output_files("", 100), ensure_ascii=False, indent=1)

    # FastMCP matches one path segment per {param}, so register the template
    # at each depth outputs uses: <category>/<file> and <category>/<sub>/<file>.
    def _read_text(path: str) -> str:
        target = _resolve_output(path)
        if target.suffix.lower() in _OUTPUT_IMAGE_MIME:
            return f"{path} is an image — read it as outputs://image/{path}"
        return target.read_text(encoding="utf-8", errors="replace")

    @mcp.resource("outputs://file/{category}/{name}", mime_type="text/plain",
                  description="A text file under knowledge_base/outputs (report, review, diagram source).")
    def output_file(category: str, name: str) -> str:
        return _read_text(f"{category}/{name}")

    @mcp.resource("outputs://file/{category}/{sub}/{name}", mime_type="text/plain",
                  description="A text file two levels under knowledge_base/outputs (e.g. pipeline/<file>.md).")
    def output_file_nested(category: str, sub: str, name: str) -> str:
        return _read_text(f"{category}/{sub}/{name}")

    @mcp.resource("outputs://image/{category}/{name}", mime_type="image/png",
                  description="An image under knowledge_base/outputs (diagram render, generated picture).")
    def output_image(category: str, name: str) -> bytes:
        return _resolve_output(f"{category}/{name}").read_bytes()

    @mcp.resource("outputs://image/{category}/{sub}/{name}", mime_type="image/png",
                  description="An image two levels under knowledge_base/outputs.")
    def output_image_nested(category: str, sub: str, name: str) -> bytes:
        return _resolve_output(f"{category}/{sub}/{name}").read_bytes()

    # --- Registry tools exposed directly: the plugins, and MR writes ---------
    # (a pack's are added once its server is up: add_pack_tools)
    from meeseek.tools.registry import _INDEX

    _register_registry_tools(mcp, tuple(n for n in plugin_tools() + _DIRECT_WRITE_TOOLS if n in _INDEX))

    return mcp


# Writes exposed straight to MCP clients, bypassing the orchestrator, so a
# caller sees them in its tool list; ask_meeseek could make the same changes,
# so this adds no permission. Other writes (Confluence, Jira) go through
# ask_meeseek and its skill's SOP — a ticket filed directly would skip the
# sprint skill's ticket format.
#
# Security: anything that can reach this server can change MRs under its
# GitLab credentials with no SOP in the way, and this endpoint has no auth.
# The server binds 127.0.0.1 unless told otherwise; bound wider, put auth in
# front (SECURITY.md).
_DIRECT_WRITE_TOOLS: tuple[str, ...] = (
    "update_mr", "create_mr", "comment_mr", "comment_mr_lines", "reply_mr_comment",
)


def plugin_tools() -> tuple[str, ...]:
    """meeseek's plugins: every registry tool that changes nothing outside this
    machine's outputs (lookups, drawing, review_page), and every read of a
    running pack — one stateless call, no agent turn. Both MCP clients and the
    Claude sessions meeseek hands work to get these; the Claude hand-off tools
    themselves are excluded, and so are reading meeseek's own past
    conversations (registry.SESSION_TOOLS), its tasks (registry.TASK_TOOLS) and
    its reminders (registry.REMIND_TOOLS — an MCP caller is not the person)
    and the spec review (registry.SPEC_TOOLS — model calls an MCP caller would
    spend unbounded)."""
    from meeseek.tools.registry import (CLAUDE_TOOLS, GENERAL_TOOLS, REMIND_TOOLS, SESSION_TOOLS, SPEC_TOOLS,
                                        TASK_TOOLS)

    own = tuple(n for n in GENERAL_TOOLS
                if n not in CLAUDE_TOOLS + SESSION_TOOLS + TASK_TOOLS + REMIND_TOOLS + SPEC_TOOLS)
    return own + tuple(n for n in pack_reads() if n not in own)


def pack_reads() -> tuple[str, ...]:
    """The read tools of the packs running now (servers for every skill)."""
    from meeseek.app import current

    a = current()
    tools = a.mcp_tools if a is not None else None
    if tools is None:
        return ()
    effects = tools.effects()
    shared = {s.name for s in tools.servers if "*" in s.agents}
    return tuple(sorted(n for n, e in effects.items() if e == "read" and tools.server_of(n) in shared))


async def add_pack_tools(mcp) -> list[str]:
    """Offer the running packs' plugins (their reads) and direct writes on
    this MCP server, each answering through meeseek's session with the pack.
    Called once the packs are up; returns the names added."""
    from meeseek.app import app

    tools = app().mcp_tools
    if tools is None:
        return []
    have = {t.name for t in await mcp.list_tools()}
    wanted = set(pack_reads()) | set(_DIRECT_WRITE_TOOLS)
    added = []
    for spec in tools.specs():
        if spec.name in wanted and spec.name not in have:
            async def call(_name=spec.name, **kwargs) -> str:
                out = await app().mcp_tools.call(_name, kwargs)
                text = "\n".join(c.get("text", "") for c in out.get("content", []) if c.get("type") == "text")
                if out.get("isError"):
                    raise RuntimeError(text)
                return text

            _register(mcp, spec.name, spec.description, call, spec.parameters)
            added.append(spec.name)
    return added


def _register_registry_tools(mcp, names: tuple[str, ...]) -> None:
    """One MCP tool per registry tool named: same name, description and
    parameter schema as the agents get."""
    import functools

    from meeseek.tools.registry import tool_specs

    for name, description, fn, schema in tool_specs(*names):
        async def call(_fn=fn, **kwargs) -> str:
            # Off the event loop: these are blocking calls.
            return await asyncio.to_thread(functools.partial(_fn, **kwargs))

        _register(mcp, name, description, call, schema)


def _register(mcp, name: str, description: str, call, schema: dict) -> None:
    """One MCP tool: `call` under `name`, with the arguments `schema` names."""
    import inspect

    from meeseek.tools.registry import signature_params

    props = schema.get("properties", {})
    required = schema.get("required", [])
    params = signature_params(schema)
    call.__signature__ = inspect.Signature(params, return_annotation=str)
    call.__annotations__ = {p.name: p.annotation for p in params} | {"return": str}
    call.__name__ = name
    # The parameter descriptions live in the schema, and FastMCP reads only
    # the signature — so fold them into the description or the caller is left
    # guessing what `mr_iid` or `append_description` mean.
    arg_help = "\n".join(
        f"    {p}: {spec.get('description', '')}"
        + ("" if p in required else "  (optional)")
        for p, spec in props.items()
    )
    mcp.add_tool(call, name=name, description=f"{description}\n\nArgs:\n{arg_help}")
