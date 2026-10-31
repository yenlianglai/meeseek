"""A model that answers from a script: for tests, evals and demos.

    model = Scripted(say("hi"), call("c1", "kb_grep", pattern="x"), say("found it"))

Each step returns the next scripted response and records what it was shown
(`seen`: the system text and the conversation as messages), so a test can
assert what the model would have read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from meeseek.core.events import Event, ModelStepped, ToolCall
from meeseek.core.tools import ToolSpec
from meeseek.core.turn import Message, conversation


@dataclass(frozen=True)
class Step:
    text: str = ""
    calls: tuple[ToolCall, ...] = ()
    error: BaseException | None = None
    cut_off: bool = False


def say(text: str) -> Step:
    return Step(text=text)


def call(call_id: str, name: str, **args: Any) -> Step:
    return Step(calls=(ToolCall(id=call_id, name=name, args=args),))


def cut_off(call_id: str, name: str, **args: Any) -> Step:
    """A call the model's output limit cut short."""
    return Step(calls=(ToolCall(id=call_id, name=name, args=args),), cut_off=True)


def fail(error: BaseException) -> Step:
    return Step(error=error)


@dataclass
class Seen:
    system: str
    messages: list[Message]
    tools: list[str]
    descriptions: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)       # a one-shot call's schema, temperature, …
    files: list[tuple[str, bytes]] = field(default_factory=list)  # what the call attached: (mime, bytes)


@dataclass
class Scripted:
    steps: list[Step] = field(default_factory=list)
    seen: list[Seen] = field(default_factory=list)
    agent: str = "scripted"
    on_step: Callable[[int], None] | None = None

    def __init__(self, *steps: Step, agent: str = "scripted") -> None:
        self.steps, self.seen, self.agent, self.on_step = list(steps), [], agent, None

    async def step(self, *, system: str, history: Sequence[Event], tools: Sequence[ToolSpec],
                   session: str, turn: str, **options: Any) -> ModelStepped:
        from meeseek.adapters.llm import _load

        messages = conversation(history)
        self.seen.append(Seen(system=system, messages=messages, tools=[t.name for t in tools],
                              descriptions={t.name: t.description for t in tools},
                              options={k: v for k, v in options.items() if v is not None},
                              files=[(a.mime, _load(a.ref)) for m in messages for a in m.attachments
                                     if a.ref.startswith("mem:")]))
        if self.on_step:
            self.on_step(len(self.seen))
        if not self.steps:
            raise AssertionError("the script ran out")
        s = self.steps.pop(0)
        if s.error is not None:
            raise s.error
        return ModelStepped(session=session, turn=turn, by=self.agent, text=s.text, calls=s.calls, cut_off=s.cut_off)
