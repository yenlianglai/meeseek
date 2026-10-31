"""The console's API on meeseek's engine: its sessions and its turns' events
(surfaces/console.py), from the event store."""

import asyncio
import base64
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meeseek import engine
from meeseek.adapters.scripted import Scripted, call, say
from meeseek.runtime import orchestrator
from meeseek.surfaces import console


@pytest.fixture
def client(monkeypatch):
    from meeseek.config import config

    monkeypatch.setattr(config(), "mcp_servers", [])
    app = FastAPI()
    console.install(app)
    return TestClient(app)


def _models(monkeypatch, *steps):
    router = Scripted(*steps, agent="router")
    monkeypatch.setattr(engine, "build_model", lambda name, by="": router if by == orchestrator.name()
                        else Scripted(agent=by))
    return router


def _frames(res):
    return [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: ")]


def _turn(client, sid, text, user="webui", files=()):
    return _frames(client.post(f"/api/sessions/{sid}/turns", json={"user": user, "text": text, "files": list(files)}))


def test_a_turn_streams_its_steps_results_and_reply(client, monkeypatch):
    _models(monkeypatch, call("c", "list_claude_sessions"), say("沒有進行中的工作。"))
    frames = _turn(client, "web-1", "Claude 在忙嗎？")
    assert [f["kind"] for f in frames] == ["step", "result", "reply"]
    assert frames[0]["calls"][0]["name"] == "list_claude_sessions"
    assert frames[1]["call"] == {"id": "c", "name": "list_claude_sessions"} and not frames[1]["error"]
    assert frames[2]["text"] == "沒有進行中的工作。" and frames[2]["by"] == orchestrator.name()
    assert len({f["turn"] for f in frames}) == 1


def test_a_reply_sent_back_shows_only_as_shipped_and_the_policy_says_why(client, monkeypatch):
    _models(monkeypatch, say("我晚點再回報"), say("我晚點再回報"))
    frames = _turn(client, "web-2", "看一下")
    replies = [f for f in frames if f["kind"] == "reply"]
    assert len(replies) == 1 and replies[0]["text"].startswith("我晚點再回報") and "⚠️" in replies[0]["text"]
    assert {f["policy"] for f in frames if f["kind"] == "policy"} == {"promises"}


def test_a_results_text_is_unwrapped_from_its_envelope():
    assert console.result_text({"content": [{"type": "text", "text": '{"a": 1}'}], "isError": True}) == (
        '{\n  "a": 1\n}', True)
    assert console.result_text({"result": "42 tickets"}) == ("42 tickets", False)
    assert console.result_text({"error": "HTTP 403"}) == ("HTTP 403", True)
    assert console.result_text("plain") == ("plain", False)


def test_sessions_are_listed_with_their_title_read_and_deleted_by_owner(client, monkeypatch):
    _models(monkeypatch, say("好。"), say("好。"))
    _turn(client, "web-a", "看 sprint 進度")
    _turn(client, "inbox-a", "hi", user="inbox")
    listed = client.get("/api/sessions", params={"user": "webui"}).json()
    assert [(s["id"], s["title"]) for s in listed] == [("web-a", "看 sprint 進度")]
    s = client.get("/api/sessions/web-a").json()
    assert [e["kind"] for e in s["events"]] == ["said", "reply"] and s["events"][0]["text"] == "看 sprint 進度"
    new = client.post("/api/sessions", json={}).json()
    assert new["id"].startswith("web-") and new["title"] == ""
    assert client.delete("/api/sessions/web-a").status_code == 204
    assert client.get("/api/sessions").json() == []


def test_resume_carries_on_a_stopped_turn_and_says_when_there_is_none(client, monkeypatch):
    from meeseek.core import events as ev

    m = _models(monkeypatch, say("查到了：12 張。"))
    c = ev.ToolCall(id="c1", name="list_claude_sessions")
    engine.store().append([ev.UserSaid(session="web-9", turn="t9", by="webui", text="還有幾張？"),
                           ev.ModelStepped(session="web-9", turn="t9", by=orchestrator.name(), calls=(c,)),
                           ev.ToolRan(session="web-9", turn="t9", by=orchestrator.name(), call=c, result="none")])
    frames = _frames(client.post("/api/sessions/web-9/resume", json={"user": "webui"}))
    assert frames[-1] == {**frames[-1], "kind": "reply", "text": "查到了：12 張。", "turn": "t9"}
    assert [mm.text for mm in m.seen[0].messages if mm.role == "user"] == ["還有幾張？"]
    again = _frames(client.post("/api/sessions/web-9/resume", json={}))
    assert again[0]["kind"] == "error" and "nothing to resume" in again[0]["message"]


def test_files_reach_the_model_and_come_back_by_url(client, monkeypatch):
    m = _models(monkeypatch, say("看到了。"))
    png = b"\x89PNG\r\n\x1a\nxx"
    _turn(client, "web-3", "這張圖？", files=[{"mime": "image/png", "data": base64.b64encode(png).decode()}])
    shown = m.seen[0].messages[-1]
    assert shown.text == "這張圖？" and [a.mime for a in shown.attachments] == ["image/png"]
    [said] = [e for e in client.get("/api/sessions/web-3").json()["events"] if e["kind"] == "said"]
    [f] = said["files"]
    assert f["mime"] == "image/png"
    got = client.get(f["url"])
    assert got.content == png and got.headers["content-type"] == "image/png"
    assert client.get("/api/files/..%2Fetc").status_code in (400, 404)


def test_a_turn_with_nothing_in_it_is_refused(client):
    assert client.post("/api/sessions/web-4/turns", json={"text": " "}).status_code == 400


# --- watching and running are separate things ---------------------------------------


class Gated:
    """A scripted model a test can hold mid-turn: each set() of the gate lets
    exactly one step through, so the turn hangs before the next one."""

    def __init__(self, *steps: object, agent: str = "router") -> None:
        self.inner = Scripted(*steps, agent=agent)
        self.gate = asyncio.Event()
        self.gate.set()

    async def step(self, **kw):
        await self.gate.wait()
        self.gate.clear()
        return await self.inner.step(**kw)


class Sse:
    """One streaming request straight to the ASGI app — httpx's ASGITransport
    buffers a response whole, which would hide the frames of a turn still
    running. Frames land in `frames` as they are sent; hang_up() is the real
    client's abort: a disconnect, nothing more."""

    def __init__(self, app, method: str, url: str, body: dict | None = None) -> None:
        path, _, query = url.partition("?")
        self._scope = {"type": "http", "http_version": "1.1", "method": method, "scheme": "http",
                       "path": path, "raw_path": path.encode(), "query_string": query.encode(),
                       "root_path": "", "headers": [(b"content-type", b"application/json")],
                       "client": ("test", 1), "server": ("test", 80)}
        self._body: bytes | None = json.dumps(body).encode() if body is not None else b""
        self._gone = asyncio.Event()
        self._buf = ""
        self.frames: list[dict] = []
        self.status = 0
        self.task = asyncio.ensure_future(app(self._scope, self._receive, self._send))

    async def _receive(self) -> dict:
        if self._body is not None:
            body, self._body = self._body, None
            return {"type": "http.request", "body": body, "more_body": False}
        await self._gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self._buf += message.get("body", b"").decode()
            *full, self._buf = self._buf.split("\n\n")
            self.frames += [json.loads(line[6:]) for f in full for line in f.split("\n")
                            if line.startswith("data: ")]

    def hang_up(self) -> None:
        self._gone.set()


async def _until(cond, what: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        assert loop.time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


def _app(monkeypatch) -> FastAPI:
    from meeseek.config import config

    monkeypatch.setattr(config(), "mcp_servers", [])
    app = FastAPI()
    console.install(app)
    return app


def _http(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://console")


def _gated(monkeypatch, *steps) -> Gated:
    router = Gated(*steps, agent=orchestrator.name())
    monkeypatch.setattr(engine, "build_model", lambda name, by="": router if by == orchestrator.name()
                        else Scripted(agent=by))
    return router


def test_a_dropped_stream_detaches_and_the_turn_runs_to_its_end(monkeypatch):
    """Switching away (or closing the tab) only stops watching: the turn keeps
    going and its events keep landing in the store."""
    async def run():
        m = _gated(monkeypatch, call("c", "list_claude_sessions"), say("跑完了。"))
        app = _app(monkeypatch)
        async with _http(app) as http:
            s = Sse(app, "POST", "/api/sessions/web-d/turns", {"user": "webui", "text": "跑個長的"})
            await _until(lambda: any(f["kind"] == "result" for f in s.frames), "the first call's result")
            s.hang_up()                           # the turn hangs at the gate; the watcher leaves
            await asyncio.wait_for(s.task, 5)
            listed = (await http.get("/api/sessions", params={"user": "webui"})).json()
            assert [(row["id"], row["running"]) for row in listed] == [("web-d", True)], "the turn runs on"
            m.gate.set()
            from meeseek.core.events import Replied

            await _until(lambda: any(isinstance(e, Replied) for e in engine.store().read("web-d")),
                         "the turn's reply in the store")
            listed = (await http.get("/api/sessions", params={"user": "webui"})).json()
            assert listed[0]["running"] is False
            [reply] = [e for e in engine.store().read("web-d") if isinstance(e, Replied)]
            assert reply.text == "跑完了。"

    asyncio.run(run())


def test_stop_cancels_the_turn_and_resume_carries_it_on(monkeypatch):
    async def run():
        m = _gated(monkeypatch, call("c", "list_claude_sessions"), say("查到了。"))
        app = _app(monkeypatch)
        async with _http(app) as http:
            assert (await http.post("/api/sessions/web-s/stop")).json() == {"stopped": False}, "nothing running yet"
            s = Sse(app, "POST", "/api/sessions/web-s/turns", {"user": "webui", "text": "有幾張票？"})
            await _until(lambda: any(f["kind"] == "result" for f in s.frames), "the first call's result")
            assert (await http.post("/api/sessions/web-s/stop")).json() == {"stopped": True}
            s.hang_up()
            await asyncio.wait_for(s.task, 5)     # the watcher's stream ends with the cancelled turn
            assert [f["kind"] for f in s.frames] == ["step", "result"], "no reply: the turn was cut short"
            assert (await http.get("/api/sessions/web-s")).json()["running"] is False
            m.gate.set()
            r = Sse(app, "POST", "/api/sessions/web-s/resume", {"user": "webui"})
            await asyncio.wait_for(r.task, 5)     # the stream ends itself with the finished turn
            assert r.frames[-1]["kind"] == "reply" and r.frames[-1]["text"] == "查到了。"

    asyncio.run(run())


def test_attach_replays_the_running_turns_events_then_streams_live(monkeypatch):
    async def run():
        m = _gated(monkeypatch, call("c", "list_claude_sessions"), say("兩張。"))
        app = _app(monkeypatch)
        async with _http(app) as http:
            s = Sse(app, "POST", "/api/sessions/web-a/turns", {"user": "webui", "text": "看一下"})
            await _until(lambda: any(f["kind"] == "result" for f in s.frames), "the first call's result")
            late = Sse(app, "GET", "/api/sessions/web-a/stream")
            await _until(lambda: len(late.frames) >= 2, "the catch-up replay")
            assert [f["kind"] for f in late.frames] == [f["kind"] for f in s.frames], "caught up to the live watcher"
            got = (await http.get("/api/sessions/web-a")).json()
            assert got["running"] is True and got["running_turn"] == s.frames[0]["turn"]
            m.gate.set()
            await asyncio.wait_for(asyncio.gather(s.task, late.task), 5)  # both end with the turn
            assert late.frames == s.frames, "the late watcher saw the whole turn"
            assert [f["kind"] for f in late.frames] == ["step", "result", "reply"]

    asyncio.run(run())


def test_attach_to_an_idle_session_says_there_is_nothing_to_watch(client):
    assert client.get("/api/sessions/web-idle/stream").status_code == 204


def test_the_server_serves_the_console_from_the_event_store(monkeypatch):
    from meeseek.config import config
    from meeseek.server import create_app

    monkeypatch.setattr(config(), "mcp_servers", [])
    client = TestClient(create_app(enable_scheduler=False))
    assert client.post("/api/sessions", json={}).json()["id"].startswith("web-")
    assert client.get("/api/sessions").json() == []


def test_the_server_documents_its_api(monkeypatch):
    """README → Surfaces: every route is at /docs, its schema at /openapi.json."""
    from meeseek.config import config
    from meeseek.server import create_app

    monkeypatch.setattr(config(), "mcp_servers", [])
    client = TestClient(create_app(enable_scheduler=False))
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/api/sessions", "/api/sessions/{sid}/turns", "/inbox_api/items", "/auth_api/status"} <= set(paths)
    assert client.get("/docs").status_code == 200


def _old_store(home, sid="old-1", user="webui"):
    """A sessions.db as ADK wrote it, with one conversation."""
    import sqlite3

    from meeseek.runtime.sessions import APP_NAME

    conn = sqlite3.connect(home.sessions_db)
    conn.execute("CREATE TABLE sessions (app_name TEXT, user_id TEXT, id TEXT, state TEXT, create_time REAL, "
                 "update_time REAL)")
    conn.execute("CREATE TABLE events (app_name TEXT, user_id TEXT, session_id TEXT, invocation_id TEXT, "
                 "timestamp REAL, event_data TEXT)")
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, '{}', 1.0, 5.0)", (APP_NAME, user, sid))
    for ts, author, text in ((1.0, "user", "我叫 Ann"), (2.0, "router", "好，Ann。")):
        conn.execute("INSERT INTO events VALUES (?, ?, ?, 'inv-1', ?, ?)",
                     (APP_NAME, user, sid, ts, json.dumps({"author": author, "invocation_id": "inv-1",
                                                           "content": {"parts": [{"text": text}]}})))
    conn.commit()
    conn.close()


def test_conversations_from_before_the_engine_switch_are_listed_opened_and_continued(client, monkeypatch, _home):
    _old_store(_home)
    m = _models(monkeypatch, say("你叫 Ann。"))
    assert [(s["id"], s["title"], s["updated"]) for s in client.get("/api/sessions").json()] == [
        ("old-1", "我叫 Ann", 5.0)]
    events = client.get("/api/sessions/old-1").json()["events"]
    assert [(e["kind"], e["text"]) for e in events] == [("said", "我叫 Ann"), ("reply", "好，Ann。")]
    _turn(client, "old-1", "我叫什麼？")
    assert [(mm.role, mm.text) for mm in m.seen[0].messages] == [
        ("user", "我叫 Ann"), ("assistant", "好，Ann。"), ("user", "我叫什麼？")], "the agent sees the old turns"
    assert [s["id"] for s in client.get("/api/sessions").json()] == ["old-1"], "listed once, from the new store now"
    assert client.delete("/api/sessions/old-1").status_code == 204 and client.get("/api/sessions").json() == []
