"""The seams: what meeseek's core needs from the world, as Protocols.

An adapter implements one (adapters/…); app.py picks which. A port has a
contract test that every adapter of it passes (tests/unit/test_ports.py), so
swapping one cannot quietly change behaviour.

    EventStore   the turn's events, append-only               sqlite, memory
    Lease        one leader among processes (the scheduler)   sqlite, memory
    ToolProvider the tools a turn may call                    the registry; MCP packs (S4)
    Model        one step of a model                          gemini, litellm, an OpenAI-compatible gateway
    Messenger    a phone channel                              telegram
    Source       things that arrive on their own              gmail, chat, alerts (S4)
    Calendar     meetings about to start                      google calendar (S4)
    Hands        an agent that does the work in a folder      claude code
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Protocol, Sequence, runtime_checkable

from meeseek.core.events import Event, ModelStepped
from meeseek.core.tools import ToolSpec


@runtime_checkable
class EventStore(Protocol):
    def append(self, events: Sequence[Event]) -> None:
        """Add events at the end of their sessions, in order. Never rewrites."""

    def read(self, session: str, *, after: int = 0) -> list[Event]:
        """A session's events from position `after` (0 = from the start)."""

    def sessions(self) -> list[str]:
        """Every session with an event, oldest first."""

    def delete(self, session: str) -> None:
        """Forget a session: the person deleted the conversation."""

    def prune(self, before: float) -> int:
        """Forget the sessions whose last event is before `before`; how many."""


@runtime_checkable
class Lease(Protocol):
    def acquire(self, name: str, holder: str, ttl_s: float) -> bool:
        """Take or renew `name` for `holder`; False while someone else holds it unexpired."""

    def release(self, name: str, holder: str) -> None:
        """Give it up, if `holder` has it."""

    def holder(self, name: str) -> str:
        """Who holds it now ('' when nobody does, or the lease ran out)."""


@runtime_checkable
class ToolProvider(Protocol):
    def tools(self) -> list[ToolSpec]: ...


@runtime_checkable
class Model(Protocol):
    def step(self, *, system: str, history: Sequence[Event], tools: Sequence[ToolSpec],
             session: str, turn: str) -> Awaitable[ModelStepped]:
        """One model response to the conversation so far."""


@runtime_checkable
class Messenger(Protocol):
    name: str

    def send(self, chat: str, texts: list[str]) -> Awaitable[None]: ...
    def send_files(self, chat: str, paths: list[Path]) -> Awaitable[None]: ...
    def ask(self, chat: str, item: dict, action: dict) -> Awaitable[None]: ...


@runtime_checkable
class Source(Protocol):
    name: str

    def poll(self, state: dict, now: float) -> list[dict]:
        """What arrived since `state` says it last looked; updates `state`."""


@runtime_checkable
class Calendar(Protocol):
    def upcoming(self, now: float, within_s: float) -> list[dict]:
        """Meetings starting in the next `within_s` seconds: title, start, link."""


@runtime_checkable
class Hands(Protocol):
    def run(self, task: str, *, cwd: Path, origin: dict[str, Any]) -> Awaitable[dict]:
        """Hand `task` over in `cwd`; the record of the session it ran in."""
