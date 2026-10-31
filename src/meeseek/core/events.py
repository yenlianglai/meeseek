"""What happens in a turn, as events.

A turn is the stream of these, appended in order and never rewritten: the
person said something, the model took a step (text, tool calls or both), a
tool ran or was held for a yes, a policy spoke, the turn replied. Everything
else — the conversation the model is shown next, the console's live view,
the usage ledger, the learning loop's record — is read from the stream.

Each event is frozen and carries its `kind`, so a list of them serialises to
JSON and back as itself (`dump` / `load`).
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session: str
    turn: str                     # the turn (invocation) it belongs to
    at: float = Field(default_factory=time.time)
    by: str = ""                  # the agent or person it came from


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    args: dict[str, Any] = {}
    meta: dict[str, Any] = {}     # what the provider needs back with it (Gemini's thought signature)


class Attachment(BaseModel):
    """A file that came with a message: its type, and the blob it is kept as."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mime: str
    ref: str = ""                 # "" when only its arrival is known (a legacy session's)


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


class UserSaid(_Event):
    kind: Literal["user_said"] = "user_said"
    text: str
    attachments: tuple[Attachment, ...] = ()   # images and PDFs, leading the text
    answers: str = ""             # the ask_user call this answers, when one was open


class ModelStepped(_Event):
    """One model response: what it said, and the tools it asked for."""

    kind: Literal["model_stepped"] = "model_stepped"
    text: str = ""
    calls: tuple[ToolCall, ...] = ()
    usage: Usage | None = None
    cut_off: bool = False             # it hit its output limit: a call it asked for may be cut short


class ToolRan(_Event):
    kind: Literal["tool_ran"] = "tool_ran"
    call: ToolCall
    result: Any = None
    failed: bool = False


class ToolHeld(_Event):
    """A call that did not run: waiting for the person's yes, or refused."""

    kind: Literal["tool_held"] = "tool_held"
    call: ToolCall
    reason: str = ""


class PolicySpoke(_Event):
    """A policy changed the course of the turn: replaced a result, sent a reply back, added a note."""

    kind: Literal["policy_spoke"] = "policy_spoke"
    policy: str
    said: str = ""
    to_model: bool = False        # the model reads it (a reply sent back), or it is only on the record


class Summarized(_Event):
    """The session's first `upto` events, summed up: what a model is shown in
    their place once the conversation grows long. They stay in the store."""

    kind: Literal["summarized"] = "summarized"
    summary: str
    upto: int


class Replied(_Event):
    """The turn's answer to the person."""

    kind: Literal["replied"] = "replied"
    text: str


Event = Annotated[Union[UserSaid, ModelStepped, ToolRan, ToolHeld, PolicySpoke, Replied, Summarized],
                  Field(discriminator="kind")]

_EVENTS = TypeAdapter(list[Event])
_EVENT = TypeAdapter(Event)


def dump(events: list[Event]) -> list[dict]:
    return _EVENTS.dump_python(events, mode="json")


def load(raw: list[dict]) -> list[Event]:
    return _EVENTS.validate_python(raw)


def load_one(raw: dict) -> Event:
    return _EVENT.validate_python(raw)


# --- reading a stream --------------------------------------------------------

def calls_without_results(events: list[Event]) -> list[ToolCall]:
    """Calls a model asked for that never ran nor were held: a turn cut off
    between the request and the result. Whether such a call landed is not
    known, so a write among them must be read back, not sent again."""
    settled = {e.call.id for e in events if isinstance(e, (ToolRan, ToolHeld))}
    return [c for e in events if isinstance(e, ModelStepped) for c in e.calls if c.id not in settled]


def open_question(events: list[Event]) -> ToolCall | None:
    """The ask_user call still waiting for the person: asked in the session's
    last turn, and nothing said since."""
    for e in reversed(events):
        if isinstance(e, UserSaid):
            return None
        if isinstance(e, ModelStepped):
            asked = [c for c in e.calls if c.name == "ask_user"]
            if asked:
                return asked[-1]
    return None


def last_reply(events: list[Event]) -> str:
    for e in reversed(events):
        if isinstance(e, Replied):
            return e.text
    return ""
