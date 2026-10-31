"""A pack's tool, called by name from meeseek's own code: the trigger loop's
sources, fetch_url on a Google link, the knowledge base's Drive mirror, the
meeting notes. (Agents reach a pack's tools through their turn instead.)

From a worker thread while the server runs, the call goes to the event loop
that holds the packs' sessions. With no loop holding them (a script, a test),
the call starts the packs for itself and closes them after.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

TIMEOUT_S = 1800   # a Drive sync or a recording download can take minutes


async def acall(name: str, args: dict | None = None) -> str:
    """The tool's answer as text; raises when it answers with an error."""
    from meeseek.adapters.mcp import McpTools
    from meeseek.app import app

    a = app()
    a.mcp_tools = a.mcp_tools or McpTools()
    return _text(await a.mcp_tools.call(name, dict(args or {})))


def call(name: str, args: dict | None = None) -> str:
    """`acall` from synchronous code — never from the thread running the event loop."""
    from meeseek.app import app

    tools = app().mcp_tools
    loop = getattr(tools, "_loop", None)
    if tools is not None and loop is not None and loop.is_running():
        try:
            mine = asyncio.get_running_loop()
        except RuntimeError:
            mine = None
        if mine is loop:
            raise RuntimeError(f"packs.call({name!r}) on the event loop would wait on itself: await acall")
        out = asyncio.run_coroutine_threadsafe(tools.call(name, dict(args or {})), loop).result(TIMEOUT_S)
        return _text(out)
    return _once(name, args)


def call_json(name: str, args: dict | None = None) -> Any:
    """`call`, for a tool that answers JSON."""
    return json.loads(call(name, args) or "null")


def _once(name: str, args: dict | None) -> str:
    """With no loop holding the packs: start them, call, close them."""
    from meeseek.adapters.mcp import McpTools
    from meeseek.config import cfg
    from meeseek.engine import run_sync

    async def job() -> str:
        tools = McpTools([s for s in cfg.mcp_servers if s.pack])
        try:
            return _text(await tools.call(name, dict(args or {})))
        finally:
            await tools.close()

    return run_sync(job)


def _text(out: dict) -> str:
    text = "\n".join(c.get("text", "") for c in out.get("content", []) if c.get("type") == "text")
    if out.get("isError"):
        raise RuntimeError(text[:500] or "the pack's tool failed")
    return text
