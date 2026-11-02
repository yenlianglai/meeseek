"""The web console's API (frontend/src/lib/api.ts), on meeseek's engine.

    GET    /api/sessions?user=           the user's conversations, newest first
    POST   /api/sessions                 {user} → a new, empty one
    GET    /api/sessions/{id}?user=      one, with its events
    DELETE /api/sessions/{id}?user=
    POST   /api/sessions/{id}/turns      {user, text, files: [{mime, data}]} → a stream of its events
    POST   /api/sessions/{id}/resume     {user} → the turn that stopped, carried on, as a stream
    POST   /api/sessions/{id}/stop       cancel the running turn (it can be resumed)
    GET    /api/sessions/{id}/stream     watch the running turn: what it has sent, then live; 204 when idle
    GET    /api/files/{ref}              a file a message brought
    GET    /api/workstreams              the open pieces of work (?closed=1: all), latest first
    GET    /api/workstreams/lookup       ?text=&branch=&claude_session=&cwd= → the one a
                                         Claude Code prompt is about, with its context
    GET    /api/workstreams/{id}         one, whole

A turn keeps running when its stream is dropped: closing the console, or
switching to another session, only stops watching. Only /stop cancels. A
session row carries `running`, and /stream attaches to a running turn from
its first event, so switching back loses nothing.

A session is {id, user, title, updated}: the title is its first message. A
stream is `data: {event}` lines, ended by the server closing it (the person's
own message is not echoed: the console drew it as they sent it). An event is
one of these (`kind`), each with its `turn`, `at` (epoch seconds) and `by`:

    said    {text, files: [{mime, url}], answers}   the person; `answers` names
            the question (an ask_user call) the message answers
    step    {text, calls: [{id, name, args}], model, tokens: {input, output}}
            a step that called tools, and what it said with them
    result  {call: {id, name}, text, error, held}  what a call returned — the
            text unwrapped from its envelope (MCP content, a {"result": …}),
            JSON indented; `held`: it did not run (it waits for your yes, or
            a policy answered for it)
    reply   {text}                         the answer, once, as shipped
    policy  {policy, said, to_model}       a policy's word: sent back to the
            model, or a note on the reply (the Trace shows it; the chat need not)
    retry   {attempt, of, in_s, error}     a stream only: the model is asked again
    error   {message}                      a stream only: the turn failed

A step whose text a policy sent back or annotated is not shown as written:
the reply is, once, as shipped.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from meeseek.core.events import Event, ModelStepped, PolicySpoke, Replied, ToolHeld, ToolRan, UserSaid
from meeseek.core.turn import NothingToResume

logger = logging.getLogger(__name__)

CONSOLE_USER = "webui"


def _pretty(text: str) -> str:
    """A string that is really JSON, indented; prose as it is."""
    t = text.strip()
    if not t.startswith(("{", "[")):
        return text
    try:
        return json.dumps(json.loads(t), ensure_ascii=False, indent=2)
    except ValueError:
        return text


def result_text(result: Any) -> tuple[str, bool]:
    """What a call returned, as a person reads it, and whether it reads as a
    failure: the payload out of whichever envelope it came in."""
    if isinstance(result, str):
        return _pretty(result), False
    if isinstance(result, dict):
        if isinstance(result.get("error"), str):
            return result["error"], True
        if isinstance(result.get("content"), list):          # an MCP server's
            text = "\n".join(str(p.get("text", "")) for p in result["content"] if isinstance(p, dict)).strip()
            if text:
                return _pretty(text), result.get("isError") is True
        if "result" in result:
            v = result["result"]
            return ("" if v is None else _pretty(v) if isinstance(v, str)
                    else json.dumps(v, ensure_ascii=False, indent=2, default=str)), False
    return json.dumps(result, ensure_ascii=False, indent=2, default=str), False


def console_event(e: Event) -> dict | None:
    """One stored event as the console reads it; None for what it does not show."""
    base = {"turn": e.turn, "at": e.at, "by": e.by}
    if isinstance(e, UserSaid):
        return {**base, "kind": "said", "text": e.text, "answers": e.answers,
                "files": [{"mime": a.mime, "url": f"/api/files/{a.ref}"} for a in e.attachments if a.ref]}
    if isinstance(e, ModelStepped):
        if not e.calls:
            return None                       # its text is shown as the reply (Replied)
        out: dict[str, Any] = {**base, "kind": "step", "text": e.text,
                               "calls": [{"id": c.id, "name": c.name, "args": c.args} for c in e.calls]}
        if e.usage:
            out["model"] = e.usage.model
            out["tokens"] = {"input": e.usage.input_tokens, "output": e.usage.output_tokens}
        return out
    if isinstance(e, ToolHeld):
        return {**base, "kind": "result", "call": {"id": e.call.id, "name": e.call.name}, "text": e.reason,
                "error": False, "held": True}
    if isinstance(e, ToolRan):
        text, error = result_text(e.result)
        return {**base, "kind": "result", "call": {"id": e.call.id, "name": e.call.name}, "text": text,
                "error": error or e.failed, "held": False}
    if isinstance(e, Replied):
        return {**base, "kind": "reply", "text": e.text}
    if isinstance(e, PolicySpoke):
        return {**base, "kind": "policy", "policy": e.policy, "said": e.said, "to_model": e.to_model}
    return None


def owner(events: list[Event]) -> str:
    """Whose conversation it is: who started its first turn."""
    first = next((e for e in events if isinstance(e, UserSaid)), None)
    return first.by if first else ""


def _title(events: list[Event]) -> str:
    first = next((e for e in events if isinstance(e, UserSaid) and e.text.strip()), None)
    return first.text.strip()[:80] if first else ""


def session_row(sid: str, user: str, events: list[Event], updated: float | None = None) -> dict:
    return {"id": sid, "user": user, "title": _title(events),
            "updated": updated if updated is not None else (events[-1].at if events else 0.0)}


def _files(raw: list[dict] | None) -> list[tuple[str, bytes]]:
    return [(str(f.get("mime", "")), base64.b64decode(f.get("data", ""))) for f in raw or [] if f.get("data")]


async def _body(request: Request) -> dict:
    raw = await request.body()
    return json.loads(raw) if raw.strip() else {}


class _Run:
    """One running turn: the frames it has sent so far, and who is watching.
    The frames double as the catch-up for a watcher that attaches late, so a
    turn needs no second home for its events while it runs (the store has them
    for good; this buffer has them in the stream's own shape, retries included)."""

    def __init__(self) -> None:
        self.frames: list[str] = []
        self.watchers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.turn: str = ""


def router() -> APIRouter:
    from meeseek import engine

    r = APIRouter(prefix="/api")
    runs: dict[str, _Run] = {}

    def events_of(sid: str, user: str) -> list[Event]:
        from meeseek.runtime import history

        return engine.store().read(sid) or history.legacy_events(sid, user)

    @r.get("/sessions")
    def list_sessions(user: str = CONSOLE_USER) -> list[dict]:
        """The user's conversations, newest first — those from before the
        engine switch (runtime/history.py) included."""
        from meeseek.runtime import history

        store = engine.store()
        out = []
        for sid in store.sessions():
            events = store.read(sid)
            if owner(events) == user:
                out.append({**session_row(sid, user, events), "running": sid in runs})
        mine = {s["id"] for s in out}
        for sid, updated in history.legacy_sessions(user):
            if sid not in mine:
                out.append({**session_row(sid, user, history.legacy_events(sid, user), updated), "running": False})
        return sorted(out, key=lambda s: s["updated"], reverse=True)

    @r.post("/sessions")
    async def create_session(request: Request) -> dict:
        body = await _body(request)
        return session_row(f"web-{uuid.uuid4().hex[:12]}", body.get("user") or CONSOLE_USER, [], time.time())

    @r.get("/sessions/{sid}")
    def get_session(sid: str, user: str = CONSOLE_USER) -> dict:
        events = events_of(sid, user)
        run = runs.get(sid)
        return {**session_row(sid, user, events), "running": run is not None,
                "running_turn": run.turn if run else "",
                "events": [x for e in events if (x := console_event(e)) is not None]}

    @r.delete("/sessions/{sid}")
    def delete_session(sid: str, user: str = CONSOLE_USER) -> Response:
        from meeseek.runtime import history

        engine.store().delete(sid)
        history.delete_legacy(sid, user)
        return Response(status_code=204)

    @r.get("/workstreams")
    def list_workstreams(closed: bool = False) -> list[dict]:
        from meeseek.runtime import workstreams

        return workstreams.rows(include_closed=closed)

    @r.get("/workstreams/lookup")
    def lookup_workstream(text: str = "", branch: str = "", claude_session: str = "", cwd: str = "") -> dict:
        """What Claude Code's prompt hook adds (scripts/workstream_hook.py): the
        workstream the prompt or its branch names, its Claude session attached."""
        from meeseek.runtime import workstreams

        ws = workstreams.lookup(text[:4000], branch, claude_session, cwd)
        return {"id": ws["id"], "key": ws["key"], "context": workstreams.render(ws)} if ws else {}

    @r.get("/workstreams/{ws_id}")
    def get_workstream(ws_id: str) -> dict:
        from meeseek.runtime import workstreams

        ws = workstreams.get(ws_id)
        if ws is None:
            raise HTTPException(404, "no such workstream")
        return {**ws, "context": workstreams.render(ws)}

    @r.get("/files/{ref}")
    def get_file(ref: str) -> Response:
        from meeseek.adapters.blobs import blobs

        if not ref.isalnum():
            raise HTTPException(400, "not a file reference")
        try:
            data = blobs().get(ref)
        except OSError:
            raise HTTPException(404, "no such file") from None
        return Response(data, media_type=_mime(data), headers={"Cache-Control": "private, max-age=31536000"})

    def watch(run: _Run, replay: list[str]) -> StreamingResponse:
        """One watcher's stream: the frames it missed, then live to the turn's
        end. Closing it only detaches — the turn runs on; /stop cancels."""
        q: asyncio.Queue = asyncio.Queue()
        run.watchers.add(q)

        async def frames_out():
            try:
                for frame in replay:
                    yield frame
                while (frame := await q.get()) is not None:
                    yield frame
            finally:
                run.watchers.discard(q)

        return StreamingResponse(frames_out(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    def stream(sid: str, run) -> StreamingResponse:
        if sid in runs:
            frame = json.dumps({"kind": "error", "message": "this conversation is already running a turn"},
                               ensure_ascii=False)
            return StreamingResponse(iter([f"data: {frame}\n\n"]), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache"})
        this = runs[sid] = _Run()

        def send(payload: dict) -> None:
            frame = f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            this.frames.append(frame)
            for q in this.watchers:
                q.put_nowait(frame)

        def observe(e: Event) -> None:
            this.turn = this.turn or e.turn   # which turn is running, for a reader catching up
            if isinstance(e, UserSaid):
                return                        # the console drew the person's message as they sent it
            if (x := console_event(e)) is not None:
                send(x)

        def retrying(attempt: int, of: int, delay: float, error: str) -> None:
            send({"kind": "retry", "attempt": attempt, "of": of, "in_s": delay, "error": error})

        async def go() -> None:
            try:
                await run(observe, retrying)
            except asyncio.CancelledError:
                raise                         # an explicit stop: the turn pauses, to resume
            except NothingToResume:
                send({"kind": "error", "message": "nothing to resume: this conversation's last turn finished"})
            except Exception as e:  # noqa: BLE001 — the console shows it; the turn is over
                from meeseek.runtime.llm import failure_message

                logger.warning("turn in %s failed: %s", sid, e, exc_info=True)
                send({"kind": "error", "message": failure_message(e)})
            finally:
                runs.pop(sid, None)
                for q in this.watchers:
                    q.put_nowait(None)

        this.task = asyncio.create_task(go())
        return watch(this, [])

    @r.post("/sessions/{sid}/stop")
    async def stop_turn(sid: str) -> dict:
        """Cancel the running turn — the only thing that does; it pauses where
        it is, to be resumed. Gone from `runs` on return: a new turn can start at once."""
        run = runs.get(sid)
        if run is None or run.task is None:
            return {"stopped": False}
        run.task.cancel()
        await asyncio.wait([run.task])
        return {"stopped": True}

    @r.get("/sessions/{sid}/stream")
    def attach(sid: str) -> Response:
        """Watch the running turn: every frame it has already sent, then live
        to its end. 204 when nothing is running."""
        run = runs.get(sid)
        if run is None:
            return Response(status_code=204)
        return watch(run, list(run.frames))

    @r.post("/sessions/{sid}/turns")
    async def run_turn(sid: str, request: Request) -> StreamingResponse:
        body = await _body(request)
        user, text, files = body.get("user") or CONSOLE_USER, str(body.get("text") or ""), _files(body.get("files"))
        if not text.strip() and not files:
            raise HTTPException(400, "a turn needs text or a file")
        return stream(sid, lambda observe, retrying: engine.turn(
            sid, text, by=user, watched=True, observe=observe, attachments=files, on_retry=retrying))

    @r.post("/sessions/{sid}/resume")
    async def resume_turn(sid: str, request: Request) -> StreamingResponse:
        user = (await _body(request)).get("user") or CONSOLE_USER
        return stream(sid, lambda observe, retrying: engine.resume(
            sid, by=user, watched=True, observe=observe, on_retry=retrying))

    return r


def _mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    return "application/octet-stream"


def install(app: FastAPI) -> None:
    """Serve the console's API from meeseek's engine."""
    app.include_router(router())
