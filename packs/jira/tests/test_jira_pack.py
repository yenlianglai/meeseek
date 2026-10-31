"""The Jira pack: its tools read, its settings come from the client, its JQL is built right."""

import asyncio
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import meeseek_pack_jira
from meeseek_pack_jira import jira
from meeseek_pack_kit import server

SETTINGS = {"base_url": "https://acme.atlassian.net", "ticket_prefix": "[Core]",
            "projects": [{"key": "ACME", "board": "ACME board", "component": "api"}]}


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("MEESEEK_PACK_CONFIG", json.dumps(SETTINGS))
    monkeypatch.setenv("JIRA_USERNAME", "me@acme.test")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")


@pytest.fixture
def asked(monkeypatch):
    urls = []

    def request(url, **kw):
        urls.append(url)
        return {"issues": [{"key": "ACME-1", "fields": {"summary": "Fix login", "status": {"name": "Open"}}}]}

    monkeypatch.setattr(jira.http, "request", request)
    return urls


def test_every_tool_is_a_read_with_the_projects_key_in_its_help():
    async def listed():
        srv = server("jira", meeseek_pack_jira.tools())
        async with create_connected_server_and_client_session(srv) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in asyncio.run(listed())}
    assert set(tools) == {"jira_search", "jira_get_issue", "jira_get_sprint_issues", "jira_poll"}
    assert all(t.annotations.readOnlyHint for n, t in tools.items() if n != "jira_poll")
    assert tools["jira_poll"].meta["meeseek/effect"] == "source"
    assert "ACME-123" in tools["jira_get_issue"].inputSchema["properties"]["issue_key"]["description"]


def test_placeholders_name_the_default_project():
    assert meeseek_pack_jira.placeholders() == {"jira_project_key": "ACME", "board_name": "ACME board",
                                                "jira_component": "api", "ticket_prefix": "Core"}


def test_free_text_is_searched_as_text(asked):
    out = jira.jira_search("login bug")
    assert "text+~+%22login+bug%22" in asked[0] and asked[0].startswith("https://acme.atlassian.net/rest/api/3/")
    assert "ACME-1" in out


def test_sprint_tickets_come_from_the_default_project_and_prefix(asked):
    jira.jira_get_sprint_issues()
    assert "project+%3D+ACME" in asked[0] and "%22Core%22" in asked[0]


def test_an_api_error_is_said_not_raised(monkeypatch):
    monkeypatch.setattr(jira.http, "request", lambda url, **kw: {"error": "HTTP 401: bad token", "url": url})
    assert jira.jira_get_issue("ACME-1") == "Jira API error: HTTP 401: bad token"
