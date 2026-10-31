"""A pack-shaped MCP server over stdio: tools that declare their effects, a
config handed in by the client, and placeholders — written against the bare
`mcp` SDK, so the core's tests need no pack installed."""

import asyncio
import json
import os

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("tracker-pack")
CONFIG = json.loads(os.environ.get("MEESEEK_PACK_CONFIG") or "{}")


def _tool(name, effect, raw=False):
    return types.Tool(name=name, description=f"{name}.", inputSchema={"type": "object", "properties": {}},
                      annotations=types.ToolAnnotations(readOnlyHint=effect == "read"),
                      _meta={"meeseek/effect": effect, "meeseek/shows_raw": raw})


@server.list_tools()
async def list_tools():
    return [_tool("tracker_issue", "read"), _tracker_logs(), _tool("tracker_close", "write"),
            _tool("tracker_sync", "side"), _tool("tracker_poll", "source")]


def _tracker_logs():
    return _tool("tracker_logs", "read", raw=True)


@server.call_tool(validate_input=False)
async def call_tool(name, arguments):
    if name == "tracker_poll":   # a source: what arrived since `since`, and its state for next time
        seen = (arguments.get("state") or {}).get("polls", 0)
        items = [{"kind": "alert", "key": f"tracker:{seen}", "title": "ACME-1 reopened", "sender": "tracker",
                  "received_at": arguments.get("since", 0) + 1, "body": "", "meta": {"project": "ACME"}}]
        return [types.TextContent(type="text", text=json.dumps({"items": items, "state": {"polls": seen + 1}}))]
    return [types.TextContent(type="text", text=f"{name} on {CONFIG.get('base_url', '?')}")]


@server.list_resources()
async def list_resources():
    return [types.Resource(uri="meeseek://placeholders", name="placeholders")]


@server.read_resource()
async def read_resource(uri):
    return json.dumps({"tracker_project": CONFIG.get("project", "")})


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
