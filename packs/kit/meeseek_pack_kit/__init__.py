"""What a meeseek pack is made of.

A pack is an MCP server that brings one vendor's tools — an issue tracker, a
git host, a cloud — to meeseek, or to any other MCP client. It is its own
process, so meeseek's core never imports a vendor:

    from meeseek_pack_kit import Tool, config, params, serve

    def get_issue(issue_key: str) -> str: ...

    TOOLS = [Tool("get_issue", "Read an issue.", get_issue,
                  params({"issue_key": {"type": "string"}}, ["issue_key"]))]

    if __name__ == "__main__":
        serve("tracker", TOOLS)

What each tool declares, and how it reaches the client:

    writes       changes something outside this machine: the reply must say
                 how it went, and a skill must be granted it
                 → annotations.readOnlyHint false, _meta "meeseek/effect": "write"
    side_effect  changes something, but is not a write to account for (a sync
                 into local files)        → _meta "meeseek/effect": "side"
    (neither)    a read                    → readOnlyHint true, "meeseek/effect": "read"
    shows_raw    its result is raw evidence (log lines, a pod table) the reply
                 shows                     → _meta "meeseek/shows_raw": true
    source       not for agents: meeseek's trigger loop polls it for things that
                 arrived on their own (mail, alerts)  → "meeseek/effect": "source"
    internal     not for agents either: meeseek's own code calls it by name (a
                 sync into the folder it names)  → _meta "meeseek/internal": true

A source is called with its configured arguments plus `since` (epoch seconds:
report what arrived after it) and `state` (what it returned as state last
time, {} at first), and answers {"items": [...], "state": {...}}. An item:

    kind         mail | chat | alert | meeting — what meeseek does with it
    key          unique and stable: the same thing polled twice has one key
    title, sender, received_at (epoch seconds), link, body
    meta         what the kind's prompt needs (a mail's thread_id, …)

Settings come from the client: meeseek passes a pack's `config:` block from
project.yaml as JSON in MEESEEK_PACK_CONFIG, and a folder of the pack's own
for what it keeps between calls (a git mirror) in MEESEEK_PACK_DATA; secrets
are plain environment variables. `placeholders` (a function returning {name: text}) is served as
the resource meeseek://placeholders — values a skill's instructions may name
as {{name}}, such as the issue tracker's project key.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from dataclasses import dataclass
from typing import Any

CONFIG_ENV = "MEESEEK_PACK_CONFIG"
DATA_ENV = "MEESEEK_PACK_DATA"
PLACEHOLDERS_URI = "meeseek://placeholders"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    fn: Callable[..., Any]
    parameters: dict
    writes: bool = False
    side_effect: bool = False
    shows_raw: bool = False
    source: bool = False
    internal: bool = False

    @property
    def effect(self) -> str:
        return "source" if self.source else "write" if self.writes else "side" if self.side_effect else "read"


def config() -> dict:
    """This pack's settings, as the client passed them ({} when it passed none)."""
    raw = os.environ.get(CONFIG_ENV, "").strip()
    return json.loads(raw) if raw else {}


def data_dir() -> Path:
    """Where this pack keeps what it caches between calls (made on first use)."""
    raw = os.environ.get(DATA_ENV, "").strip()
    path = Path(raw) if raw else Path.home() / ".cache" / "meeseek-packs" / Path(sys.argv[0]).stem
    path.mkdir(parents=True, exist_ok=True)
    return path


def secret(name: str) -> str:
    return os.environ.get(name, "").strip()


def params(properties: dict, required: list[str] | None = None) -> dict:
    """A JSON-schema object for a tool's arguments."""
    return {"type": "object", "properties": properties, "required": list(required or [])}


def _as_text(out: Any) -> str:
    if isinstance(out, str):
        return out
    return json.dumps(out, ensure_ascii=False, default=str)


def server(name: str, tools: list[Tool], *, instructions: str = "",
           placeholders: Callable[[], dict] | None = None):
    """The MCP server for these tools (low-level, so each keeps its own schema)."""
    import mcp.types as types
    from mcp.server.lowlevel import Server

    by_name = {t.name: t for t in tools}
    if len(by_name) != len(tools):
        raise ValueError(f"pack {name}: two tools share a name")
    srv = Server(name, instructions=instructions or None)

    @srv.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(name=t.name, description=t.description, inputSchema=t.parameters,
                           annotations=types.ToolAnnotations(readOnlyHint=t.effect == "read",
                                                             destructiveHint=t.writes),
                           _meta={"meeseek/effect": t.effect, "meeseek/shows_raw": t.shows_raw,
                                  "meeseek/internal": t.internal or t.source})
                for t in tools]

    @srv.call_tool(validate_input=False)
    async def call_tool(tool: str, arguments: dict) -> list[types.TextContent]:
        t = by_name.get(tool)
        if t is None:
            raise ValueError(f"no tool named {tool!r}")
        if inspect.iscoroutinefunction(t.fn):
            out = await t.fn(**(arguments or {}))
        else:   # vendor calls block: off the event loop
            out = await asyncio.to_thread(lambda: t.fn(**(arguments or {})))
        return [types.TextContent(type="text", text=_as_text(out))]

    if placeholders is not None:
        @srv.list_resources()
        async def list_resources() -> list[types.Resource]:
            return [types.Resource(uri=PLACEHOLDERS_URI, name="placeholders", mimeType="application/json")]

        @srv.read_resource()
        async def read_resource(uri) -> str:
            if str(uri) != PLACEHOLDERS_URI:
                raise ValueError(f"no resource {uri}")
            return json.dumps(placeholders(), ensure_ascii=False)

    return srv


def serve(name: str, tools: list[Tool], *, instructions: str = "",
          placeholders: Callable[[], dict] | None = None) -> None:
    """Serve the tools over stdio until the client hangs up."""
    from mcp.server.stdio import stdio_server

    srv = server(name, tools, instructions=instructions, placeholders=placeholders)

    async def run() -> None:
        async with stdio_server() as (read, write):
            await srv.run(read, write, srv.create_initialization_options())

    asyncio.run(run())
