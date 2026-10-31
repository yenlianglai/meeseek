"""MCP servers as tools for meeseek's engine (the official `mcp` SDK).

Each server in project.yaml `mcp_servers` is one long-lived session: a stdio
process (`command` + `args`, `${VAR}` filled from the environment), a pack
from packs/ (`pack: jira` runs `python -m meeseek_pack_jira`, handed its
`config:` as JSON in MEESEEK_PACK_CONFIG), or a streamable-HTTP endpoint
(`url`; `auth: adc` signs every request with Google Application Default
Credentials, as Google-managed *.googleapis.com/mcp expects). Its tools —
those `tool_filter` names, or all of them — become ToolSpecs whose call goes
through that session. A result keeps MCP's shape ({"content": [...],
"isError": …}), which the result cap knows how to trim.

What a tool does to the world comes from the tool: a pack's say so in `_meta`
("meeseek/effect": read | write | side), any other server's in its
`readOnlyHint` annotation, and failing both, from its name (harness.is_write).
A pack's sources ("meeseek/effect": source) and internal tools
("meeseek/internal": true) are no agent's tools and not on meeseek's MCP
surface: meeseek's own code calls them by name (runtime/packs.py).

A server that cannot start (a missing variable, no credentials, a crash) is
left out with a warning; the agent runs without its tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import AsyncExitStack
from typing import Any

from meeseek.config import MCPServerConfig, cfg
from meeseek.core.tools import Effect, ToolSpec

logger = logging.getLogger(__name__)

_ENV = re.compile(r"\$\{([^}]+)\}")
_ADC_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
START_TIMEOUT_S = 60


def interpolate(value: str) -> str:
    return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), value)


PACK_CONFIG_ENV = "MEESEEK_PACK_CONFIG"
PACK_DATA_ENV = "MEESEEK_PACK_DATA"
PLACEHOLDERS_URI = "meeseek://placeholders"


def _stdio_params(server: MCPServerConfig):
    from mcp.client.stdio import StdioServerParameters

    if server.pack:
        command = server.command or sys.executable
        args = list(server.args) or ["-m", f"meeseek_pack_{server.pack.replace('-', '_')}"]
        from meeseek.config.paths import home

        config = json.dumps(server.config, ensure_ascii=False)
        data = str(home().data / "packs" / server.name)
        return StdioServerParameters(command=command, args=args, env={
            **os.environ, **_env(server), PACK_CONFIG_ENV: config, PACK_DATA_ENV: data})
    args = list(server.args)
    for template in server.args_from_env:
        filled = interpolate(template)
        if filled.split("=", 1)[-1]:
            args.append(filled)
    return StdioServerParameters(command=server.command, args=args, env={**os.environ, **_env(server)})


def _env(server: MCPServerConfig) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, template in server.env.items():
        value = interpolate(template)
        if not value:
            raise RuntimeError(f"env var for {key} is empty")
        env[key] = value
    return env


def effect_of(tool: Any) -> str:
    """read | write | side, as the tool declares it; "" when it does not."""
    declared = (getattr(tool, "meta", None) or {}).get("meeseek/effect")
    if declared in ("read", "write", "side", "source"):
        return declared
    read_only = getattr(getattr(tool, "annotations", None), "readOnlyHint", None)
    if read_only is True:
        return "read"
    if read_only is False:
        return "write"
    return ""


def _adc_auth(server: MCPServerConfig):
    import google.auth
    import google.auth.transport.requests
    import httpx

    if server.impersonate:
        from meeseek.config.auth import get_impersonated_credentials

        credentials = get_impersonated_credentials(interpolate(server.impersonate), _ADC_SCOPES)
    else:
        credentials, _ = google.auth.default(scopes=_ADC_SCOPES)
    quota = interpolate(server.quota_project) or cfg.google_cloud.quota_project

    class ADC(httpx.Auth):
        def auth_flow(self, request):
            if not credentials.valid:
                credentials.refresh(google.auth.transport.requests.Request())
            request.headers["Authorization"] = f"Bearer {credentials.token}"
            if quota:
                request.headers["x-goog-user-project"] = quota
            yield request

    return ADC()


class McpServer:
    """One server's session, held open by a task of its own until `close()`."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.session: Any = None
        self.tools: list[ToolSpec] = []
        self.hidden: set[str] = set()
        self.effects: dict[str, str] = {}
        self.placeholders: dict[str, str] = {}
        self._ready = asyncio.Event()
        self._done = asyncio.Event()
        self._error: BaseException | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> list[ToolSpec]:
        self._task = asyncio.create_task(self._hold(), name=f"mcp:{self.config.name}")
        try:
            await asyncio.wait_for(self._ready.wait(), START_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._error = TimeoutError(f"did not start in {START_TIMEOUT_S}s")
        if self._error is not None:
            await self.close()
            raise RuntimeError(f"MCP server {self.config.name!r}: {self._error}") from self._error
        return self.tools

    async def _hold(self) -> None:
        from mcp import ClientSession

        try:
            async with AsyncExitStack() as stack:
                if self.config.url:
                    from mcp.client.streamable_http import streamablehttp_client

                    auth = _adc_auth(self.config) if self.config.auth == "adc" else None
                    read, write, _ = await stack.enter_async_context(streamablehttp_client(
                        interpolate(self.config.url),
                        headers={k: interpolate(v) for k, v in self.config.headers.items()} or None,
                        timeout=30, auth=auth))
                else:
                    from mcp.client.stdio import stdio_client

                    read, write = await stack.enter_async_context(stdio_client(_stdio_params(self.config)))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = (await session.list_tools()).tools
                wanted = set(self.config.tool_filter)
                self.session = session
                kept = [t for t in listed if not wanted or t.name in wanted]
                self.effects = {t.name: e for t in kept if (e := effect_of(t))}
                self.hidden = {t.name for t in kept if self.effects.get(t.name) == "source"
                               or (getattr(t, "meta", None) or {}).get("meeseek/internal")}
                self.tools = [self._spec(t) for t in kept if t.name not in self.hidden]
                self.placeholders = await self._placeholders(session)
                self._ready.set()
                await self._done.wait()
        except BaseException as e:  # noqa: BLE001 — reported to start(); the agent runs without these tools
            self._error = e
            self._ready.set()

    @staticmethod
    async def _placeholders(session: Any) -> dict[str, str]:
        """The values a pack offers for {{placeholders}} in skill instructions."""
        try:
            listed = (await session.list_resources()).resources
        except Exception:  # noqa: BLE001 — a server without resources
            return {}
        if not any(str(r.uri) == PLACEHOLDERS_URI for r in listed):
            return {}
        res = await session.read_resource(PLACEHOLDERS_URI)
        try:
            values = json.loads(res.contents[0].text)
        except (ValueError, AttributeError, IndexError):
            return {}
        return {str(k): str(v) for k, v in values.items()} if isinstance(values, dict) else {}

    def _spec(self, tool: Any) -> ToolSpec:
        from meeseek.runtime.harness import is_write

        name = tool.name
        declared = self.effects.get(name)
        writes = declared == "write" if declared else is_write(name)

        async def call(**args: Any) -> dict:
            return await self.call(name, args)

        return ToolSpec(name=name, description=tool.description or "", fn=call,
                        parameters=tool.inputSchema or {"type": "object", "properties": {}},
                        effect=Effect.WRITE if writes else Effect.READ,
                        shows_raw=bool((getattr(tool, "meta", None) or {}).get("meeseek/shows_raw")))

    async def call(self, name: str, args: dict) -> dict:
        result = await self.session.call_tool(name, args)
        return result.model_dump(mode="json", exclude_none=True)

    async def close(self) -> None:
        self._done.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 10)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                self._task.cancel()


class McpTools:
    """Every configured server, started once per event loop; tools by agent."""

    def __init__(self, servers: list[MCPServerConfig] | None = None) -> None:
        self.servers = list(cfg.mcp_servers if servers is None else servers)
        self._running: dict[str, McpServer] = {}
        self._failed: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    async def tools_for(self, agent: str, grants: tuple[str, ...] = (), *, named: bool = True) -> list[ToolSpec]:
        """A skill's tools from every server that serves it: all of a server
        that names the skill (when `named`: the skill said `mcp: true`); of a
        server for every skill ("*"), its reads, and the writes the skill's
        `tools:` grant."""
        await self._start()
        shared: list[ToolSpec] = []
        own: list[ToolSpec] = []
        for s in self.servers:
            if s.name not in self._running:
                continue
            if named and agent in s.agents:
                own += self._running[s.name].tools
            elif "*" in s.agents:
                shared += [t for t in self._running[s.name].tools if not t.writes or t.name in grants]
        return shared + own     # a server the skill names wins a name both serve

    async def call(self, name: str, args: dict) -> dict:
        """Call a tool by name on whichever running server has it (a source, the
        daily plan) — a pack's first, as no skill's own server is meant."""
        await self._start()
        servers = sorted(self._running.values(), key=lambda s: "*" not in s.config.agents)
        for server in servers:
            if name in server.hidden or any(t.name == name for t in server.tools):
                return await server.call(name, args)
        raise LookupError(f"no running MCP server has a tool named {name!r}")

    def effects(self) -> dict[str, str]:
        """read | write | side of every tool a running server declared."""
        return {n: e for server in self._running.values() for n, e in server.effects.items()}

    def placeholders(self) -> dict[str, str]:
        return {k: v for server in self._running.values() for k, v in server.placeholders.items()}

    def raw_tools(self) -> set[str]:
        """The tools whose result is raw evidence the reply shows (a pack's shows_raw)."""
        return {t.name for server in self._running.values() for t in server.tools if t.shows_raw}

    def tool_names(self) -> set[str]:
        """Every tool a running server has, its hidden ones included."""
        return {n for server in self._running.values() for n in [*(t.name for t in server.tools), *server.hidden]}

    def specs(self) -> list[ToolSpec]:
        return [t for server in self._running.values() for t in server.tools]

    def server_of(self, tool: str) -> str:
        """The name of the running server that has this tool ("" if none)."""
        return next((name for name, server in self._running.items() if any(t.name == tool for t in server.tools)), "")

    async def _start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:          # a new loop (each CLI run): the old sessions died with theirs
            self._running, self._failed, self._loop, self._lock = {}, set(), loop, asyncio.Lock()
        async with self._lock:
            for s in self.servers:
                if s.name in self._running or s.name in self._failed:
                    continue
                server = McpServer(s)
                try:
                    await server.start()
                    self._running[s.name] = server
                    logger.info("MCP server %s: %d tool(s)", s.name, len(server.tools))
                except Exception as e:  # noqa: BLE001
                    logger.warning("MCP server %s left out: %s", s.name, e)
                    self._failed.add(s.name)

    async def close(self) -> None:
        for server in self._running.values():
            await server.close()
        self._running = {}
