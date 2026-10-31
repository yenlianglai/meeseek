"""A pack serves its tools with their effects, and its placeholders, over MCP."""

import asyncio
import json

from mcp.shared.memory import create_connected_server_and_client_session

from meeseek_pack_kit import PLACEHOLDERS_URI, Tool, config, params, server


def _tools():
    async def slow(x: str) -> str:
        return f"async {x}"

    return [
        Tool("get_issue", "Read an issue.", lambda issue_key: f"{issue_key}: open",
             params({"issue_key": {"type": "string", "description": "e.g. ACME-1"}}, ["issue_key"])),
        Tool("create_issue", "Open an issue.", lambda summary: {"key": "ACME-2", "summary": summary},
             params({"summary": {"type": "string"}}, ["summary"]), writes=True),
        Tool("sync_docs", "Copy docs here.", lambda: "synced", params({}), side_effect=True),
        Tool("query_logs", "Logs.", slow, params({"x": {"type": "string"}}), shows_raw=True),
    ]


def _session(fn, placeholders=None):
    async def go():
        srv = server("tracker", _tools(), placeholders=placeholders)
        async with create_connected_server_and_client_session(srv) as client:
            return await fn(client)

    return asyncio.run(go())


def test_each_tool_says_what_it_does_to_the_world():
    async def listed(client):
        return {t.name: t for t in (await client.list_tools()).tools}

    tools = _session(listed)
    assert tools["get_issue"].annotations.readOnlyHint is True
    assert tools["get_issue"].inputSchema["properties"]["issue_key"]["description"] == "e.g. ACME-1"
    assert tools["create_issue"].annotations.readOnlyHint is False and tools["create_issue"].meta["meeseek/effect"] == "write"
    assert tools["sync_docs"].meta["meeseek/effect"] == "side"
    assert tools["query_logs"].meta["meeseek/shows_raw"] is True


def test_a_call_runs_the_tool_and_answers_text():
    async def called(client):
        a = await client.call_tool("get_issue", {"issue_key": "ACME-1"})
        b = await client.call_tool("create_issue", {"summary": "s"})
        c = await client.call_tool("query_logs", {"x": "y"})
        return a, b, c

    a, b, c = _session(called)
    assert a.content[0].text == "ACME-1: open" and not a.isError
    assert json.loads(b.content[0].text) == {"key": "ACME-2", "summary": "s"}
    assert c.content[0].text == "async y"


def test_a_failing_tool_is_an_error_result_not_a_crash():
    async def called(client):
        return await client.call_tool("get_issue", {})

    out = _session(called)
    assert out.isError


def test_placeholders_are_a_resource():
    async def read(client):
        res = await client.read_resource(PLACEHOLDERS_URI)
        return json.loads(res.contents[0].text)

    assert _session(read, placeholders=lambda: {"jira_project_key": "ACME"}) == {"jira_project_key": "ACME"}


def test_config_comes_from_the_client(monkeypatch):
    monkeypatch.setenv("MEESEEK_PACK_CONFIG", '{"base_url": "https://x"}')
    assert config() == {"base_url": "https://x"}
    monkeypatch.delenv("MEESEEK_PACK_CONFIG")
    assert config() == {}
