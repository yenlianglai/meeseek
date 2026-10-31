"""One meeseek: its config, its home, and the state its parts share at runtime.

`build_app()` makes one. `install()` makes it the process's (the server does,
at startup); `active()` makes it this context's, so two apps can run side by
side in one process, each with its own config, files, channels and hooks.
Code that needs the running app asks `app()`.

The state here is what wires the parts together while they run — who is told
when a turn ends, which phone channels are up and how the sources are doing,
the Inbox's turn slots, the MCP surface's tasks. Caches and locks keyed by
the thing they guard (a file, a repo, a cluster) stay with their module.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator

from meeseek.config import Config, config, set_config, using_config
from meeseek.config.paths import Home, home, set_home, using_home

Hook = Callable[[dict], Awaitable[None]]


@dataclass(eq=False)
class App:
    config: Config
    home: Home
    # Called with the item after each of its turns, failed ones included — how
    # an item that came from your phone gets its answer back there.
    after_inbox_turn: list[Hook] = field(default_factory=list)
    # Called with the record when a Claude Code session meeseek drove ends.
    after_claude_turn: list[Hook] = field(default_factory=list)
    channels: dict[str, Any] = field(default_factory=dict)            # name → messengers.Channel
    channel_status: dict[str, dict] = field(default_factory=dict)     # per channel, the Inbox page's source health
    trigger_status: dict[str, dict] = field(default_factory=dict)     # per source, likewise
    answered: dict[str, asyncio.Event] = field(default_factory=dict)  # item id → set when its answer went out
    inbox_slots: asyncio.Semaphore | None = None                      # made on first use, in the running loop
    mcp_tasks: dict[str, dict] = field(default_factory=dict)
    mcp_session_locks: dict[tuple[str, str], asyncio.Lock] = field(default_factory=dict)
    mcp_tools: Any = None                                             # adapters.mcp.McpTools, made on first use

    def install(self) -> App:
        """This app for the whole process: its config and home included."""
        global _process
        set_config(self.config)
        set_home(self.home)
        _process = self
        return self

    @contextmanager
    def active(self) -> Iterator[App]:
        """This app for the current context only (an asyncio task inherits it)."""
        token = _scoped.set(self)
        try:
            with using_config(self.config), using_home(self.home):
                yield self
        finally:
            _scoped.reset(token)


def build_app(config_: Config | None = None, home_: Home | None = None) -> App:
    """An app from a config and a home; the current ones when not given."""
    return App(config=config_ or config(), home=home_ or home())


_scoped: ContextVar[App | None] = ContextVar("meeseek_app", default=None)
_process: App | None = None


def app() -> App:
    """The running app: this context's, else the process's, else one built now from the current config and home."""
    current = _scoped.get() or _process
    if current is None:
        current = build_app().install()
    return current


def current() -> App | None:
    """The running app if there is one; never builds one."""
    return _scoped.get() or _process


def uninstall() -> None:
    global _process
    _process = None
