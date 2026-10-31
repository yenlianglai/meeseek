"""Every pack starts as its own MCP server over stdio — the way meeseek runs it
(`python -m meeseek_pack_<name>`) — and lists its tools with their effects.
Listing needs no network: what a call reaches is each pack's own tests'."""

import asyncio
import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PACKS = {
    "jira": {"base_url": "https://acme.atlassian.net", "projects": [{"key": "ACME"}]},
    "gitlab": {"base_url": "https://gitlab.example.com", "projects": ["acme/app"]},
    "gcp": {"environments": {"uat": {"context": "gke_p_r_c", "namespace": "n", "gcp_project": "p"}}},
    "google": {"folder_ids": []},
    "github": {"repos": ["octo/app"]},
}


async def _listed(pack: str, config: dict, tmp_path) -> tuple[dict, dict]:
    params = StdioServerParameters(command=sys.executable, args=["-m", f"meeseek_pack_{pack}"], env={
        "MEESEEK_PACK_CONFIG": json.dumps(config), "MEESEEK_PACK_DATA": str(tmp_path), "PATH": ""})
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {t.name: t for t in (await session.list_tools()).tools}
        resources = {str(r.uri) for r in (await session.list_resources()).resources} if pack != "google" else set()
        placeholders = {}
        if "meeseek://placeholders" in resources:
            placeholders = json.loads((await session.read_resource("meeseek://placeholders")).contents[0].text)
        return tools, placeholders


@pytest.mark.parametrize("pack", sorted(PACKS))
def test_a_pack_starts_and_says_what_each_tool_does(pack, tmp_path):
    tools, placeholders = asyncio.run(_listed(pack, PACKS[pack], tmp_path))
    assert tools, pack
    for name, t in tools.items():
        effect = (t.meta or {}).get("meeseek/effect")
        assert effect in ("read", "write", "side", "source"), (pack, name)
        assert t.annotations.readOnlyHint is (effect == "read"), (pack, name)
    if pack == "jira":
        assert placeholders["jira_project_key"] == "ACME"
    if pack == "gitlab":
        assert {n for n, t in tools.items() if t.meta["meeseek/effect"] == "write"} == {
            "comment_mr", "comment_mr_lines", "reply_mr_comment", "create_mr", "update_mr"}
    if pack == "gcp":
        assert placeholders["k8s_env_names"] == "uat" and tools["query_logs"].meta["meeseek/shows_raw"]
    if pack == "google":
        assert tools["drive_sync"].meta["meeseek/internal"] and tools["gmail_poll"].meta["meeseek/effect"] == "source"
