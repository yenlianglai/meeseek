"""The MCP server that makes meeseek callable by Claude and other hosts."""

import asyncio

import pytest

from meeseek.runtime.mcp_server import MCP_USER, build_mcp_server


def _tools():
    mcp = build_mcp_server()
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


class TestExposedSurface:
    def test_exposes_the_ask_tool_with_a_session_handle(self):
        tool = _tools()["ask_meeseek"]
        props = tool.inputSchema["properties"]
        assert {"request", "session_id", "image_paths", "use_clipboard_image",
                "wait_seconds", "caller"} == set(props)

    def test_image_args_tell_the_host_not_to_send_bytes(self):
        """An MCP host cannot emit the bytes of an image it can see, so the
        description has to point at the by-reference path instead."""
        description = _tools()["ask_meeseek"].description or ""
        assert "image_paths" in description
        assert "use_clipboard_image" in description
        assert "never" in description  # "Pass paths, never image bytes"

    def test_description_lists_the_installed_skills(self):
        """The host routes on this text, so it has to reflect what is actually
        installed — not a list hand-copied into the prompt."""
        from meeseek.runtime.skills import discover_skills

        description = _tools()["ask_meeseek"].description or ""
        assert "Specialists available:" in description
        for skill in discover_skills():
            assert f"- {skill.name}:" in description

    def test_description_is_built_before_registration(self):
        """FastMCP captures the description when the tool is registered, so
        composing it afterwards would silently ship an empty list."""
        description = _tools()["ask_meeseek"].description or ""
        assert description.index("Specialists available:") < description.index("Args:")


class TestExpandedSurface:
    def test_two_entry_tools_under_meeseeks_name(self):
        """One way in, one way to follow up; lookups skip the orchestrator."""
        assert build_mcp_server().name == "meeseek"
        names = set(_tools())
        assert {"ask_meeseek", "check_meeseek", "kb_grep", "kb_read", "kb_list"} <= names
        assert not any("agent_task" in n for n in names)

    def test_ask_returns_structured_session_handle(self):
        """session_id must be a structured field, not a footer to parse."""
        for name in ("ask_meeseek", "check_meeseek"):
            out_props = (_tools()[name].outputSchema or {}).get("properties", {})
            assert {"answer", "session_id", "handled_by", "status", "needs_input", "task_id",
                    "progress"} <= set(out_props)

    def test_background_task_roundtrip(self, monkeypatch):
        import meeseek.runtime.mcp_server as mcp_server

        async def fake(request, session_id, images=None, **kw):
            return f"answer to {request}", "code_agent", []

        monkeypatch.setattr(mcp_server, "_run_request", fake)
        mcp = build_mcp_server()

        async def flow():
            _, started = await mcp.call_tool("ask_meeseek", {"request": "hi", "wait_seconds": 0})
            assert started["status"] == "running" and started["task_id"]
            await asyncio.sleep(0)  # let the background task run
            _, checked = await mcp.call_tool(
                "check_meeseek", {"task_id": started["task_id"]})
            return started, checked

        started, checked = asyncio.run(flow())
        assert checked["status"] == "answered"
        assert checked["answer"] == "answer to hi"
        assert checked["handled_by"] == "code_agent"
        assert checked["session_id"] == started["session_id"]

    def test_checking_an_unknown_task_is_an_answer_not_an_error(self):
        mcp = build_mcp_server()
        _, out = asyncio.run(mcp.call_tool("check_meeseek", {"task_id": "task-nope"}))
        assert out["status"] == "unknown"


class TestStopAndCarryOn:
    def test_a_stopped_turn_is_carried_on_by_asking_with_no_request(self, monkeypatch):
        """ask_meeseek(session_id) with no request resumes the session's last
        turn from its last step, under the same task_id."""
        import meeseek.runtime.mcp_server as mcp_server

        calls = []

        async def fake(request, session_id, images=None, resume="", **kw):
            calls.append((request, resume))
            if not resume:
                await asyncio.sleep(30)
            return "resumed", "code_agent", []

        async def stored(task_id):
            rec = next(r for r in mcp_server._task_store().read() if r["task_id"] == task_id)
            return rec, "inv-1", "interrupted"

        monkeypatch.setattr(mcp_server, "_run_request", fake)
        monkeypatch.setattr(mcp_server, "_stored_turn", stored)
        mcp = build_mcp_server()

        async def flow():
            _, started = await mcp.call_tool("ask_meeseek", {
                "request": "review !1", "session_id": "s1", "wait_seconds": 0})
            await asyncio.sleep(0)
            _, stopped = await mcp.call_tool("check_meeseek", {"task_id": started["task_id"], "stop": True})
            _, again = await mcp.call_tool("ask_meeseek", {"session_id": "s1", "wait_seconds": 5})
            return started, stopped, again

        started, stopped, again = asyncio.run(flow())
        assert stopped["status"] == "interrupted" and "ask_meeseek" in stopped["answer"]
        assert again["status"] == "answered" and again["answer"] == "resumed"
        assert again["task_id"] == started["task_id"]
        assert calls == [("review !1", ""), ("", "inv-1")]

    def test_carrying_on_resumes_the_stopped_turn_itself_not_a_new_one(self, monkeypatch):
        from meeseek import engine
        from meeseek.core.turn import TurnResult
        from meeseek.runtime import mcp_server

        seen = []

        async def resume(session, **kw):
            seen.append(session)
            return TurnResult(reply="carried on", turn="inv-1", held=[], events=[])

        async def turn(*a, **kw):
            pytest.fail("a new turn was started")

        monkeypatch.setattr(engine, "resume", resume)
        monkeypatch.setattr(engine, "turn", turn)
        final, _, _ = asyncio.run(mcp_server._run_request("", "s1", resume="inv-1"))
        assert final == "carried on" and seen == ["s1"]

    def test_no_request_and_no_session_is_an_answer(self):
        _, out = asyncio.run(build_mcp_server().call_tool("ask_meeseek", {}))
        assert out["status"] == "failed" and "request" in out["answer"]


class TestIsolation:
    def test_mcp_conversations_use_their_own_user(self):
        """Keeps them out of the console's session list, which lists `webui`."""
        from meeseek.runtime.usage import WORKFLOW_USER

        assert MCP_USER not in ("webui", WORKFLOW_USER)


class TestFailureHandling:
    def test_a_failed_request_answers_the_caller_instead_of_500ing(self, monkeypatch):
        """An MCP host shows a tool error as a dead end; a sentence it can read
        and react to is more useful."""
        import meeseek.runtime.mcp_server as mcp_server

        async def boom(request, session_id, images=None, **kw):
            raise RuntimeError("bedrock exploded")

        monkeypatch.setattr(mcp_server, "_run_request", boom)
        mcp = build_mcp_server()
        out = asyncio.run(mcp.call_tool("ask_meeseek", {"request": "hi"}))
        text = str(out)
        assert "bedrock exploded" in text and "RuntimeError" in text


class TestImageAttachment:
    """The bytes are read server-side; the caller only ever names an image."""

    def _png(self, tmp_path):
        path = tmp_path / "shot.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"pixels" * 10)
        return path

    def test_a_named_path_reaches_the_turn_as_an_inline_part(self, monkeypatch, tmp_path):
        import meeseek.runtime.mcp_server as mcp_server

        seen = {}

        async def fake(request, session_id, images=None, **kw):
            seen["images"] = images
            return "looked at it", "orchestrator", []

        monkeypatch.setenv("AGENT_IMAGE_ROOTS", str(tmp_path))
        monkeypatch.setattr(mcp_server, "_run_request", fake)
        mcp = build_mcp_server()
        _, out = asyncio.run(mcp.call_tool("ask_meeseek", {
            "request": "這張圖哪裡有問題", "image_paths": [str(self._png(tmp_path))]}))

        assert out["answer"] == "looked at it"
        assert seen["images"] == [("image/png", b"\x89PNG\r\n\x1a\n" + b"pixels" * 10)]

    def test_a_rejected_image_answers_instead_of_running_the_turn(self, monkeypatch, tmp_path):
        """Answering about a picture that was never attached is worse than
        saying it could not be attached."""
        import meeseek.runtime.mcp_server as mcp_server

        ran = []

        async def fake(request, session_id, images=None, **kw):
            ran.append(request)
            return "should not happen", "orchestrator", []

        monkeypatch.setenv("AGENT_IMAGE_ROOTS", str(tmp_path))
        monkeypatch.setattr(mcp_server, "_run_request", fake)
        mcp = build_mcp_server()
        _, out = asyncio.run(mcp.call_tool("ask_meeseek", {
            "request": "看這張", "image_paths": ["/etc/hosts"]}))

        assert not ran
        assert out["answer"].startswith("No image was attached")

    def test_starting_a_task_reads_the_clipboard_before_backgrounding(
            self, monkeypatch, tmp_path):
        """The clipboard is live state — by the time a queued task ran it could
        hold something else."""
        import meeseek.runtime.mcp_server as mcp_server
        import meeseek.runtime.images as images_mod

        order = []

        def fake_clipboard():
            order.append("clipboard")
            return "image/png", b"\x89PNG\r\n\x1a\nclip"

        async def fake(request, session_id, images=None, **kw):
            order.append(("turn", images))
            return "done", "orchestrator", []

        monkeypatch.setattr(images_mod, "read_clipboard_image", fake_clipboard)
        monkeypatch.setattr(mcp_server, "_run_request", fake)
        mcp = build_mcp_server()

        async def flow():
            _, started = await mcp.call_tool("ask_meeseek", {
                "request": "看剪貼簿那張", "use_clipboard_image": True, "wait_seconds": 0})
            assert order[0] == "clipboard"     # read before the task was queued
            await asyncio.sleep(0)
            return await mcp.call_tool("check_meeseek", {"task_id": started["task_id"]})

        _, checked = asyncio.run(flow())
        assert checked["status"] == "answered"
        assert order[1] == ("turn", [("image/png", b"\x89PNG\r\n\x1a\nclip")])


class TestStructuredStatus:
    def test_an_open_ask_user_question_is_needs_input(self, monkeypatch):
        """needs_input comes from the ask_user call's own arguments, not from
        reading question marks out of prose."""
        import meeseek.runtime.mcp_server as mcp_server

        async def fake(request, session_id, images=None, **kw):
            return "我需要確認一件事。", "acme_app_orchestrator", ["驗收標準是什麼？\n建議：p95 < 2s"]

        monkeypatch.setattr(mcp_server, "_run_request", fake)
        mcp = build_mcp_server()
        _, out = asyncio.run(mcp.call_tool("ask_meeseek", {"request": "幫我改一下延遲"}))
        assert out["status"] == "needs_input"
        assert out["needs_input"] == ["驗收標準是什麼？\n建議：p95 < 2s"]

    def test_a_reply_ending_in_a_question_is_still_an_answer(self, monkeypatch):
        import meeseek.runtime.mcp_server as mcp_server

        async def fake(request, session_id, images=None, **kw):
            return "MR !126 有三個問題要修。要我先改哪個？", "code_agent", []

        monkeypatch.setattr(mcp_server, "_run_request", fake)
        mcp = build_mcp_server()
        _, out = asyncio.run(mcp.call_tool("ask_meeseek", {"request": "看 MR"}))
        assert out["status"] == "answered" and out["needs_input"] == []


class TestProgressAndWait:
    def test_ask_hands_back_a_task_when_the_wait_runs_out(self, monkeypatch):
        import meeseek.runtime.mcp_server as mcp_server

        async def slow(request, session_id, images=None, progress=None, **kw):
            progress["lines"].append("+0s code_agent → search_code({...})")
            progress["tool_calls"] += 1
            await asyncio.sleep(0.3)
            return "finally", "code_agent", []

        monkeypatch.setattr(mcp_server, "_run_request", slow)
        mcp = build_mcp_server()

        async def flow():
            _, first = await mcp.call_tool("ask_meeseek", {"request": "review", "wait_seconds": 1})
            return first

        first = asyncio.run(flow())
        assert first["status"] == "answered" and first["answer"] == "finally"

    def test_timeout_path_returns_running_with_progress(self, monkeypatch):
        import meeseek.runtime.mcp_server as mcp_server

        async def slow(request, session_id, images=None, progress=None, **kw):
            progress["lines"].append("+0s code_agent → get_mr({\"mr_iid\": 126})")
            progress["tool_calls"] += 1
            await asyncio.sleep(0.5)
            return "finally", "code_agent", []

        real_wait_for = asyncio.wait_for

        async def quick_wait_for(aw, timeout):
            return await real_wait_for(aw, 0.05)

        monkeypatch.setattr(mcp_server, "_run_request", slow)
        monkeypatch.setattr(mcp_server.asyncio, "wait_for", quick_wait_for)
        mcp = build_mcp_server()

        async def flow():
            _, first = await mcp.call_tool("ask_meeseek", {"request": "review", "wait_seconds": 30})
            assert first["status"] == "running" and first["task_id"]
            assert "get_mr" in first["answer"]
            _, mid = await mcp.call_tool("check_meeseek", {"task_id": first["task_id"]})
            assert mid["status"] == "running" and mid["tool_calls"] == 1 and mid["progress"]
            await asyncio.sleep(0.6)
            _, done = await mcp.call_tool("check_meeseek", {"task_id": first["task_id"]})
            return done

        done = asyncio.run(flow())
        assert done["status"] == "answered" and done["answer"] == "finally"
        assert done["session_id"]

    def test_a_caller_that_passes_no_wait_is_not_left_blocking(self, monkeypatch):
        """預設值必須是有限的等待:呼叫端(尤其 LLM host)通常不傳 wait_seconds,
        預設若無限等,一個長的 routed turn 會把呼叫端卡上好幾分鐘。"""
        import meeseek.runtime.mcp_server as mcp_server

        assert mcp_server._DEFAULT_WAIT_SECONDS > 0, (
            "_DEFAULT_WAIT_SECONDS 回到 0 = 無限等 — 不傳參數的呼叫端會被卡住"
        )

        async def never_finishes(request, session_id, images=None, progress=None, **kw):
            progress["lines"].append("+0s code_agent → get_job_log({...})")
            progress["tool_calls"] += 1
            await asyncio.sleep(30)
            return "eventually", "code_agent", []

        seen: list[float | None] = []
        real_wait_for = asyncio.wait_for

        async def capture_wait_for(aw, timeout):
            seen.append(timeout)          # 記下實際生效的 timeout
            return await real_wait_for(aw, 0.05)

        monkeypatch.setattr(mcp_server, "_run_request", never_finishes)
        monkeypatch.setattr(mcp_server.asyncio, "wait_for", capture_wait_for)
        mcp = build_mcp_server()

        async def flow():
            # 注意:沒有傳 wait_seconds —— 走的就是預設值那條路
            _, res = await mcp.call_tool("ask_meeseek", {"request": "為什麼 CI 掛了"})
            return res

        res = asyncio.run(flow())
        assert res["status"] == "running", "預設呼叫沒有在時限內讓出,呼叫端會一直等"
        assert res["task_id"], "讓出了卻沒給 task_id — 背景工作就撈不回來了"
        assert seen == [mcp_server._DEFAULT_WAIT_SECONDS], (
            f"生效的 timeout 是 {seen},不是預設的 {mcp_server._DEFAULT_WAIT_SECONDS}"
        )

    def test_progress_records_calls_and_marks_failures(self):
        from meeseek.core import events as ev
        from meeseek.runtime.mcp_server import _new_progress, _note_core_event

        p = _new_progress()
        c = ev.ToolCall(id="c1", name="get_file", args={"file_path": "a.py"})
        _note_core_event(p, ev.ModelStepped(session="s", turn="t", by="code_agent", calls=(c,)))
        _note_core_event(p, ev.ToolRan(session="s", turn="t", call=c, result={"result": "x"}, failed=True))
        _note_core_event(p, ev.Replied(session="s", turn="t", text="done"))
        assert p["tool_calls"] == 1 and p["last_text"] == "done"
        assert "get_file(" in p["lines"][0] and p["lines"][1].endswith("✗")


class TestCallerScope:
    def test_caller_scopes_the_user_id(self):
        from meeseek.runtime.mcp_server import _user_for

        assert _user_for("") == MCP_USER
        assert _user_for("claude-code/acme-app") == "mcp:claude-code-acme-app"

    def test_same_session_runs_one_turn_at_a_time(self, monkeypatch):
        import meeseek.runtime.mcp_server as mcp_server

        active = {"now": 0, "max": 0}

        async def fake(request, session_id, images=None, **kw):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            await asyncio.sleep(0.05)
            active["now"] -= 1
            return "ok", "code_agent", []

        monkeypatch.setattr(mcp_server, "_run_request", fake)
        mcp = build_mcp_server()

        async def flow():
            await asyncio.gather(
                mcp.call_tool("ask_meeseek", {"request": "a", "session_id": "s1"}),
                mcp.call_tool("ask_meeseek", {"request": "b", "session_id": "s1"}),
            )
        asyncio.run(flow())
        assert active["max"] == 1


class TestOutputs:
    def test_list_and_resources(self, monkeypatch, tmp_path):
        import meeseek.runtime.mcp_server as mcp_server

        out = tmp_path / "outputs"
        (out / "reports").mkdir(parents=True)
        (out / "images").mkdir()
        (out / "reports" / "r.md").write_text("# report")
        (out / "images" / "i.png").write_bytes(b"\x89PNGdata")
        monkeypatch.setattr(mcp_server, "_outputs_dir", lambda: out)
        mcp = build_mcp_server()

        async def flow():
            _, listed = await mcp.call_tool("list_outputs", {})
            files = listed["result"] if isinstance(listed, dict) else listed
            uris = {f["path"]: f["uri"] for f in files}
            assert uris["reports/r.md"] == "outputs://file/reports/r.md"
            assert uris["images/i.png"] == "outputs://image/images/i.png"
            text = await mcp.read_resource("outputs://file/reports/r.md")
            img = await mcp.read_resource("outputs://image/images/i.png")
            return text, img

        text, img = asyncio.run(flow())
        assert "# report" in str(list(text)[0].content)
        assert list(img)[0].content == b"\x89PNGdata" or "PNG" in str(list(img)[0].content)

    def test_path_traversal_is_refused(self, monkeypatch, tmp_path):
        import meeseek.runtime.mcp_server as mcp_server

        (tmp_path / "outputs").mkdir()
        monkeypatch.setattr(mcp_server, "_outputs_dir", lambda: tmp_path / "outputs")
        import pytest

        with pytest.raises(ValueError):
            mcp_server._resolve_output("../secret.txt")
