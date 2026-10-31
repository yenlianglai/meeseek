"""MCP servers as the engine's tools: a real stdio server, started and called."""

import asyncio
import sys
from pathlib import Path

from meeseek.adapters.mcp import McpTools
from meeseek.config import MCPServerConfig, config

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_tracker.py"


def _tracker(**over):
    return MCPServerConfig(name="tracker", command=sys.executable, args=[str(SERVER)],
                           tool_filter=["jira_get_issue", "jira_create_issue"], agents=["sprint"], **over)


def test_a_servers_filtered_tools_are_offered_and_called(monkeypatch):
    monkeypatch.setattr(config(), "mcp_servers", [_tracker()])

    async def go():
        mcp = McpTools()
        try:
            tools = {t.name: t for t in await mcp.tools_for("sprint")}
            assert set(tools) == {"jira_get_issue", "jira_create_issue"}
            assert await mcp.tools_for("code") == []
            assert tools["jira_create_issue"].writes and not tools["jira_get_issue"].writes
            out = await tools["jira_get_issue"].fn(issue_key="ACME-1")
            assert out["content"][0]["text"] == "ACME-1: In Review" and not out.get("isError")
        finally:
            await mcp.close()

    asyncio.run(go())


def test_a_server_that_cannot_start_is_left_out(monkeypatch):
    broken = MCPServerConfig(name="broken", command=sys.executable, args=["-c", "import sys; sys.exit(3)"],
                             agents=["sprint"])
    monkeypatch.setattr(config(), "mcp_servers", [broken, _tracker()])

    async def go():
        mcp = McpTools()
        try:
            assert {t.name for t in await mcp.tools_for("sprint")} == {"jira_get_issue", "jira_create_issue"}
        finally:
            await mcp.close()

    asyncio.run(go())


def test_a_skill_reads_through_its_mcp_server_in_a_turn(monkeypatch):
    from meeseek import engine
    from meeseek.adapters.scripted import Scripted, call, say
    from meeseek.runtime import orchestrator

    monkeypatch.setattr(config(), "mcp_servers", [_tracker()])
    sprint = Scripted(call("g", "jira_get_issue", issue_key="ACME-1"), say("ACME-1 在 Review。"), agent="sprint_agent")
    router = Scripted(call("t", "transfer_to_agent", agent_name="sprint_agent"), agent="router")
    monkeypatch.setattr(engine, "build_model",
                        lambda name, by="": router if by == orchestrator.name() else sprint if by == "sprint_agent"
                        else Scripted(agent=by))

    async def go():
        from meeseek.app import app

        try:
            res = await engine.turn("web-mcp", "ACME-1 怎麼了？")
            assert res.reply == "ACME-1 在 Review。"
            assert "jira_get_issue" in sprint.seen[0].tools and "jira_delete_everything" not in sprint.seen[0].tools
            assert "ACME-1: In Review" in sprint.seen[1].messages[-1].text
        finally:
            await app().mcp_tools.close()

    asyncio.run(go())


PACK = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_pack.py"


def _pack():
    from meeseek.config.loader import _mcp_servers

    [server] = _mcp_servers({"tracker": {"pack": "tracker", "command": sys.executable, "args": [str(PACK)],
                                         "config": {"base_url": "https://tracker.test", "project": "ACME"}}})
    return server


def test_a_pack_serves_every_skill_its_reads_and_only_granted_writes(monkeypatch):
    server = _pack()
    assert server.agents == ["*"]
    monkeypatch.setattr(config(), "mcp_servers", [server])

    async def go():
        mcp = McpTools()
        try:
            plain = {t.name: t for t in await mcp.tools_for("code", named=False)}
            assert set(plain) == {"tracker_issue", "tracker_logs", "tracker_sync"}   # a side effect is no write
            granted = {t.name for t in await mcp.tools_for("sprint", ("tracker_close",))}
            assert "tracker_poll" not in plain, "a source is no agent's tool"
            assert granted == {"tracker_issue", "tracker_logs", "tracker_sync", "tracker_close"}
            assert plain["tracker_logs"].shows_raw and not plain["tracker_issue"].shows_raw
            out = await plain["tracker_issue"].fn()
            assert out["content"][0]["text"] == "tracker_issue on https://tracker.test"
            assert mcp.effects() == {"tracker_issue": "read", "tracker_logs": "read",
                                     "tracker_close": "write", "tracker_sync": "side", "tracker_poll": "source"}
            assert mcp.placeholders() == {"tracker_project": "ACME"}
            assert (await mcp.call("tracker_sync", {}))["content"][0]["text"] == "tracker_sync on https://tracker.test"
        finally:
            await mcp.close()

    asyncio.run(go())


def test_the_harness_takes_a_packs_word_for_what_writes(monkeypatch):
    from meeseek.app import build_app
    from meeseek.runtime.harness import has_effect, is_write

    class Running:
        def effects(self):
            return {"tracker_close": "write", "tracker_sync": "side", "tracker_list_x": "read"}

    a = build_app()
    a.mcp_tools = Running()
    with a.active():
        assert is_write("tracker_close") and not is_write("tracker_sync") and not is_write("tracker_list_x")
        assert has_effect("tracker_sync") and not has_effect("tracker_list_x")


def test_a_packs_placeholders_reach_the_instructions():
    from meeseek.app import build_app
    from meeseek.context.prompts import render

    class Running:
        def placeholders(self):
            return {"tracker_project": "ACME"}

    a = build_app()
    a.mcp_tools = Running()
    with a.active():
        assert render("project {{tracker_project}}") == "project ACME"
    assert render("project {{tracker_project}}") == "project {{tracker_project}}"


def test_meeseeks_mcp_surface_offers_a_packs_reads_once_it_is_up(monkeypatch):
    """External clients and Claude sessions see a pack's lookups as meeseek's
    plugins; its writes stay behind ask_meeseek and the skills' SOPs."""
    from meeseek.app import build_app
    from meeseek.runtime.mcp_server import add_pack_tools, build_mcp_server

    monkeypatch.setattr(config(), "mcp_servers", [_pack()])
    a = build_app()

    async def go():
        a.mcp_tools = McpTools()
        try:
            await a.mcp_tools.tools_for("code", named=False)
            surface = build_mcp_server()
            before = {t.name for t in await surface.list_tools()}
            added = await add_pack_tools(surface)
            after = {t.name for t in await surface.list_tools()}
            assert set(added) == {"tracker_issue", "tracker_logs"} and after - before == set(added)
            assert not {"tracker_close", "tracker_sync", "tracker_poll"} & after
            monkeypatch.setattr("meeseek.runtime.mcp_server._DIRECT_WRITE_TOOLS", ("tracker_close",))
            assert set(await add_pack_tools(build_mcp_server())) == {"tracker_issue", "tracker_logs", "tracker_close"}
            out = await surface.call_tool("tracker_issue", {})
            return out
        finally:
            await a.mcp_tools.close()

    with a.active():
        out = asyncio.run(go())
    assert "tracker_issue on https://tracker.test" in str(out)


def test_a_packs_source_is_polled_for_trigger_items(monkeypatch):
    """The trigger loop asks a pack's source what arrived since the last poll,
    keeps the state it hands back, and files nothing twice."""
    from meeseek.app import build_app
    from meeseek.runtime import inbox, triggers

    monkeypatch.setattr(config(), "mcp_servers", [_pack()])
    monkeypatch.setattr(config().triggers, "sources", {"tracker": {"tool": "tracker_poll", "every_s": 300}})
    assert "tracker" in triggers.enabled() and triggers._every("tracker") == 300
    a = build_app()

    async def go():
        a.mcp_tools = McpTools()
        try:
            await a.mcp_tools.tools_for("code", named=False)
            state = {}
            first = await asyncio.to_thread(triggers._poller("tracker"), state, 1000.0)
            inbox.file(**first[0])
            again = await asyncio.to_thread(triggers._poller("tracker"), {"tracker": {"pack": {}}}, 1000.0)
            return first, state, again
        finally:
            await a.mcp_tools.close()

    with a.active():
        first, state, again = asyncio.run(go())
    [item] = first
    assert (item["source"], item["kind"], item["key"]) == ("tracker", "alert", "tracker:0")
    assert state["tracker"]["pack"] == {"polls": 1} and state["tracker"]["since"] == 1000.0 - triggers._OVERLAP_S
    assert again == [], "the same key is not filed twice"
    assert triggers.decide(item)["action"] == "investigate", "an alert is work, whichever source filed it"
