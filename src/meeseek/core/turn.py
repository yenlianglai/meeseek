"""One turn: the person's message in, the reply out, every step an event.

    model step ⇄ tools — each call asks the policies first (run it, answer it
    instead, or hold it) — then the reply asks them too (ship it, send it back
    to be rewritten, or ship it with a note)

The runner knows no rule itself: a budget, a clock, an approval, a check of
the reply against what the turn did are each a Policy. It knows no model,
store or tool set either; it is given them. Every event is appended as it
happens, so the next turn reads the conversation back from the store, and a
turn cut short leaves a record the next one repairs (`conversation`).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, Sequence

from meeseek.core.events import (
    Attachment,
    Event,
    ModelStepped,
    PolicySpoke,
    Replied,
    Summarized,
    ToolCall,
    ToolHeld,
    ToolRan,
    UserSaid,
    open_question,
)
from meeseek.core.ledger import failure_of
from meeseek.core.tools import ToolSpec

logger = logging.getLogger(__name__)

UNKNOWN_OUTCOME = ("Interrupted before this call returned: whether it ran is unknown. "
                   "Check before doing it again.")

# What the runner itself says to the model, by key (the engine words them from
# the project's prompts: its `say`).
RUNNER_SAY = {
    "resumed_write": "This write was sent before the turn was interrupted; whether it took effect is unknown, so "
                     "it was not sent again. Check with a read whether it did, then decide; say in the reply that "
                     "its outcome was unknown.",
    "split_the_call": "Your last tool call was cut off by the output limit before its arguments were complete, so "
                      "it did not run. Redo it as smaller calls (write a file in parts, send a summary first); do not "
                      "send the same call again.",
    "dropped_result": "... (older tool result dropped to fit the context window; run the query again if you still "
                      "need it)",
}
UNKNOWN = "unknown"


def unknown_outcome(text: str) -> dict:
    """The result a write gets when an interruption left its outcome unknown."""
    return {"harness_status": UNKNOWN, "result": text}


def is_unknown(result: Any) -> bool:
    return isinstance(result, dict) and result.get("harness_status") == UNKNOWN


def unfinished(events: Sequence[Event]) -> str:
    """The session's last turn if it never replied — cut short by a restart, a
    dropped connection or a pause — else ""."""
    last = next((e for e in reversed(events) if isinstance(e, UserSaid)), None)
    if last is None or any(isinstance(e, Replied) and e.turn == last.turn for e in events):
        return ""
    return last.turn


# After a model refuses a request as too long, the tool results it is shown
# whole: all but the newest TRIM_KEEP[n] long ones go as a stub, harder each
# time it refuses again. The store keeps them whole.
TRIM_KEEP = (6, 1)
TRIM_MIN_CHARS = 400


def trimmed(events: Sequence[Event], keep: int, stub: str) -> list[Event]:
    ran = [i for i, e in enumerate(events) if isinstance(e, ToolRan)]
    old = set(ran[:-keep] if keep else ran)
    return [e.model_copy(update={"result": stub})
            if i in old and len(_as_text(e.result)) > TRIM_MIN_CHARS else e for i, e in enumerate(events)]


# --- what a policy can say ------------------------------------------------------

@dataclass(frozen=True)
class Run:
    """Run the call — with `args` instead of the model's, when given; `note`
    is added to what the model reads back."""
    note: str = ""
    args: dict | None = None


@dataclass(frozen=True)
class Replace:
    """Do not run it; the model reads `result` as if it had."""
    result: Any
    failed: bool = True


@dataclass(frozen=True)
class Hold:
    """Do not run it. The model reads `reason`; a `queued` call waits for the
    person's decision (TurnResult.held), any other is simply not done."""
    reason: str
    queued: bool = False


Decision = Run | Replace | Hold


@dataclass(frozen=True)
class Accept:
    pass


@dataclass(frozen=True)
class SendBack:
    """Not shipped: the model reads `message` and writes the reply again."""
    message: str


@dataclass(frozen=True)
class Annotate:
    """Shipped with `note` added, after the reply (or before it)."""
    note: str
    before: bool = False


Verdict = Accept | SendBack | Annotate


@dataclass
class TurnState:
    session: str
    turn: str
    watched: bool                     # someone is there to answer and approve
    started: float
    history: list[Event]              # the session before this turn
    agent: str = ""                   # who is answering now
    by: str = ""                      # who started it: a person's surface, or what woke it
    events: list[Event] = field(default_factory=list)   # this turn's, so far
    calls: int = 0                    # tool calls that ran or were answered by a policy
    sent_back: dict[str, int] = field(default_factory=dict)  # policy → times it sent the reply back
    memory: dict[str, Any] = field(default_factory=dict)      # a policy's own notes for this turn

    @property
    def all_events(self) -> list[Event]:
        return [*self.history, *self.events]


_CURRENT: ContextVar[TurnState | None] = ContextVar("meeseek_turn", default=None)


def current() -> TurnState | None:
    """The turn this code runs in — a tool asks, from its own thread too
    (asyncio.to_thread carries the context)."""
    return _CURRENT.get()


class Policy(Protocol):
    name: str

    async def on_tool(self, t: TurnState, call: ToolCall, spec: ToolSpec) -> Decision | None:
        """None: no opinion. The first opinion in policy order decides."""

    async def on_reply(self, t: TurnState, reply: str) -> Verdict | None: ...


class BasePolicy:
    """A policy with no opinion on anything; subclasses override what they judge."""

    name = "policy"

    async def on_tool(self, t: TurnState, call: ToolCall, spec: ToolSpec) -> Decision | None:
        return None

    def on_result(self, t: TurnState, call: ToolCall, spec: ToolSpec, result: Any) -> Any:
        """What the model reads back of a result that ran (a cap, a trim); `result` as is by default."""
        return result

    async def on_reply(self, t: TurnState, reply: str) -> Verdict | None:
        return None


@dataclass
class Agent:
    """Who answers: a model, its instruction, its tools."""

    name: str
    model: "Model"
    system: Callable[[TurnState], str] = lambda t: ""
    tools: Sequence[ToolSpec] = ()
    description: str = ""


class ContextOverflowError(Exception):
    """The model refused the request as longer than its context window."""


class NothingToResume(Exception):
    pass


class TransientModelError(Exception):
    """A model call that may well work if asked again (rate limit, 5xx, timeout)."""


class Model(Protocol):
    def step(self, *, system: str, history: Sequence[Event], tools: Sequence[ToolSpec],
             session: str, turn: str) -> Awaitable[ModelStepped]: ...


# --- the conversation a model is shown ------------------------------------------

@dataclass(frozen=True)
class Message:
    role: str                         # user | assistant | tool
    text: str = ""
    calls: tuple[ToolCall, ...] = ()
    call_id: str = ""                 # a tool message: the call it answers
    name: str = ""
    attachments: tuple[Attachment, ...] = ()


# How many of the latest messages that brought files still show them: every
# step re-sends the conversation, and old pictures crowd out the request.
KEEP_ATTACHED = 2
NO_LONGER_SHOWN = "[{n} earlier attachment(s) are no longer shown.]"


def conversation(events: Sequence[Event]) -> list[Message]:
    """The session as messages: what the person and the policies said, what the
    model said and asked for, what each call returned. A call with no result
    (a turn cut short) is answered with UNKNOWN_OUTCOME, so the conversation
    stays well formed and the model checks before redoing a write. A reply
    shipped with a note reads as shipped. Past the latest summary, the events
    it stands for are not shown: the summary is, first."""
    out: list[Message] = []
    open_calls: list[ToolCall] = []
    last = next((e for e in reversed(events) if isinstance(e, Summarized)), None)
    if last is not None:
        out.append(Message(role="user", text=last.summary))
        events = [e for e in events[last.upto:] if not isinstance(e, Summarized)]

    def close_open() -> None:
        for c in open_calls:
            out.append(Message(role="tool", text=UNKNOWN_OUTCOME, call_id=c.id, name=c.name))
        open_calls.clear()

    for e in events:
        if isinstance(e, (ToolRan, ToolHeld)):
            open_calls[:] = [c for c in open_calls if c.id != e.call.id]
            text = e.reason if isinstance(e, ToolHeld) else _as_text(e.result)
            out.append(Message(role="tool", text=text, call_id=e.call.id, name=e.call.name))
        elif isinstance(e, ModelStepped):
            close_open()
            out.append(Message(role="assistant", text=e.text, calls=e.calls))
            open_calls.extend(e.calls)
        elif isinstance(e, Replied):
            if out and out[-1].role == "assistant" and not out[-1].calls and out[-1].text != e.text:
                out[-1] = Message(role="assistant", text=e.text)
        elif isinstance(e, UserSaid):
            close_open()
            out.append(Message(role="user", text=e.text, attachments=tuple(a for a in e.attachments if a.ref)))
        elif isinstance(e, PolicySpoke) and e.to_model:
            close_open()
            out.append(Message(role="user", text=e.said))
    close_open()
    with_files = [i for i, m in enumerate(out) if m.attachments]
    for i in with_files[:-KEEP_ATTACHED] if KEEP_ATTACHED else with_files:
        m = out[i]
        note = NO_LONGER_SHOWN.format(n=len(m.attachments))
        out[i] = Message(role=m.role, text=f"{m.text}\n\n{note}".strip())
    return out


def _as_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict) and set(result) <= {"result", "tool_failed"} and isinstance(result.get("result"), str):
        return result["result"]
    import json

    return json.dumps(result, ensure_ascii=False, default=str)


# --- the runner ------------------------------------------------------------------

@dataclass
class TurnResult:
    reply: str
    turn: str
    held: list[ToolCall]
    events: list[Event]


class TurnDidNotConverge(Exception):
    pass


TRANSFER = "transfer_to_agent"
# A transient model failure is asked again after 4s, 8s, 16s … but never waits longer than this.
MAX_BACKOFF_S = 30.0


def _transfer_spec(agents: dict[str, Agent], current: str) -> ToolSpec:
    others = [a for n, a in agents.items() if n != current]
    lines = "\n".join(f"- {a.name}: {a.description}" for a in others)

    def transfer_to_agent(agent_name: str) -> str:
        return f"Transferred to {agent_name}."

    return ToolSpec(name=TRANSFER, fn=transfer_to_agent,
                    description=f"Hand this conversation to the agent that should answer it:\n{lines}",
                    parameters={"type": "object", "required": ["agent_name"], "properties": {
                        "agent_name": {"type": "string", "enum": [a.name for a in others]}}})


class TurnRunner:
    """Runs turns for one agent, or for several that hand the conversation to
    each other (`transfer_to_agent`). A turn starts with the agent that gave
    the session's last reply, else with `root`."""

    def __init__(self, *, store, model: Model | None = None, tools: Sequence[ToolSpec] = (),
                 system: Callable[[TurnState], str] = lambda t: "", agents: Sequence[Agent] = (), root: str = "",
                 policies: Sequence[Policy] = (), now: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, retries: int = 2,
                 max_steps: int = 80,
                 compactor: Callable[[TurnState], Awaitable[Summarized | None]] | None = None,
                 say: Callable[..., str] | None = None) -> None:
        self.compactor = compactor
        self.say = say or (lambda key, **values: RUNNER_SAY[key].format(**values))
        if not agents:
            agents = [Agent(name=root or "meeseek", model=model, system=system, tools=tools)]
        self.agents = {a.name: a for a in agents}
        self.root = root or agents[0].name
        self.store, self.policies = store, list(policies)
        self.now, self.sleep = now, sleep
        self.retries, self.max_steps = retries, max_steps

    @property
    def model(self) -> Model:
        return self.agents[self.root].model

    @model.setter
    def model(self, m: Model) -> None:
        self.agents[self.root].model = m

    def tools_of(self, agent: str) -> dict[str, ToolSpec]:
        specs = {t.name: t for t in self.agents[agent].tools}
        if len(self.agents) > 1:
            specs[TRANSFER] = _transfer_spec(self.agents, agent)
        return specs

    def _first_agent(self, history: list[Event]) -> str:
        for e in reversed(history):
            if isinstance(e, Replied):
                return e.by if e.by in self.agents else self.root
        return self.root

    async def run(self, session: str, text: str, *, watched: bool = True, by: str = "user",
                  observe: Callable[[Event], None] | None = None, start_with: str = "",
                  attachments: Sequence[Attachment] = (),
                  on_retry: Callable[[int, int, float, str], None] | None = None) -> TurnResult:
        """One turn. `start_with` names the agent to answer first (a slash
        command that goes straight to a skill), else whoever answered last."""
        history = self.store.read(session)
        first = start_with if start_with in self.agents else self._first_agent(history)
        t = TurnState(session=session, turn=uuid.uuid4().hex[:12], watched=watched,
                      started=self.now(), history=history, agent=first, by=by)
        t.memory["on_retry"] = on_retry
        question = open_question(history)

        def emit(e: Event) -> None:
            t.events.append(e)
            self.store.append([e])
            if observe is not None:
                observe(e)

        token = _CURRENT.set(t)
        try:
            emit(UserSaid(session=session, turn=t.turn, at=self.now(), by=by, text=text,
                          attachments=tuple(attachments), answers=question.id if question else ""))
            return await self._loop(t, emit, [])
        finally:
            _CURRENT.reset(token)

    async def resume(self, session: str, *, watched: bool = True, by: str = "",
                     observe: Callable[[Event], None] | None = None,
                     on_retry: Callable[[int, int, float, str], None] | None = None) -> TurnResult:
        """Carry on the session's last turn from its last event, if it never
        replied. A call it was waiting on is answered now: a read is run again,
        a write is marked outcome-unknown, not re-sent (ResumedWrites keeps it
        so until a read has checked)."""
        events = self.store.read(session)
        turn = unfinished(events)
        if not turn:
            raise NothingToResume(f"{session}: its last turn finished")
        mine = [e for e in events if e.turn == turn]
        history = [e for e in events if e.turn != turn]
        said = next(e for e in mine if isinstance(e, UserSaid))
        agent = next((e.by for e in reversed(mine) if isinstance(e, (ModelStepped, ToolRan)) and e.by in self.agents),
                     self._first_agent(history))
        t = TurnState(session=session, turn=turn, watched=watched, started=self.now(), history=history,
                      events=mine, agent=agent, by=by or said.by)
        t.memory["on_retry"] = on_retry
        t.calls = sum(isinstance(e, ToolRan) for e in mine)
        for e in mine:
            if isinstance(e, PolicySpoke) and e.to_model:
                t.sent_back[e.policy] = t.sent_back.get(e.policy, 0) + 1

        def emit(e: Event) -> None:
            t.events.append(e)
            self.store.append([e])
            if observe is not None:
                observe(e)

        token = _CURRENT.set(t)
        try:
            base = {"session": session, "turn": turn}
            emit(PolicySpoke(**base, at=self.now(), policy="resume", said="resumed after an interruption"))
            held: list[ToolCall] = []
            last = next((e for e in reversed(mine) if isinstance(e, ModelStepped)), None)
            answered = {e.call.id for e in mine if isinstance(e, (ToolRan, ToolHeld))}
            for call in [c for c in (last.calls if last else ()) if c.id not in answered]:
                spec = self.tools_of(t.agent).get(call.name)
                if spec is not None and spec.writes:
                    emit(ToolRan(**base, at=self.now(), by=t.agent, call=call,
                                 result=unknown_outcome(self.say("resumed_write"))))
                else:
                    for e in await self._call(t, call, base, held):
                        emit(e)
            return await self._loop(t, emit, held)
        finally:
            _CURRENT.reset(token)

    async def _loop(self, t: TurnState, emit: Callable[[Event], None], held: list[ToolCall]) -> TurnResult:
        base = {"session": t.session, "turn": t.turn}
        for _ in range(self.max_steps):
            if self.compactor is not None and (summed := await self.compactor(t)) is not None:
                emit(summed)
            trim = t.memory.get("trim", 0)
            step = await self._step(t)
            if t.memory.get("trim", 0) != trim:
                emit(PolicySpoke(**base, at=self.now(), policy="context",
                                 said=f"over the context window: older tool results shown as a stub, "
                                      f"the newest {TRIM_KEEP[t.memory['trim'] - 1]} whole"))
            emit(step)
            if step.calls and (step.cut_off or any("_unparsed" in c.args for c in step.calls)):
                for call in step.calls:     # a call cut short is no call: none of the step's run
                    emit(ToolHeld(**base, at=self.now(), by=t.agent, call=call, reason=self.say("split_the_call")))
                continue
            if step.calls:
                for call in step.calls:
                    for e in await self._call(t, call, base, held):
                        emit(e)
                continue
            reply = step.text
            verdict, by_policy = await self._judge(t, reply)
            if isinstance(verdict, SendBack):
                t.sent_back[by_policy] = t.sent_back.get(by_policy, 0) + 1
                emit(PolicySpoke(**base, at=self.now(), policy=by_policy, said=verdict.message, to_model=True))
                continue
            if isinstance(verdict, Annotate):
                reply = f"{verdict.note}\n\n{reply}" if verdict.before else f"{reply}\n\n{verdict.note}"
                emit(PolicySpoke(**base, at=self.now(), policy=by_policy, said=verdict.note))
            emit(Replied(**base, at=self.now(), by=step.by, text=reply))
            return TurnResult(reply=reply, turn=t.turn, held=held, events=list(t.events))
        raise TurnDidNotConverge(f"no reply after {self.max_steps} steps")

    async def _step(self, t: TurnState) -> ModelStepped:
        agent = self.agents[t.agent]
        attempt = 0
        while True:
            level = t.memory.get("trim", 0)
            history = t.all_events if not level else trimmed(t.all_events, TRIM_KEEP[level - 1],
                                                              self.say("dropped_result"))
            try:
                step = await agent.model.step(system=agent.system(t), history=history,
                                              tools=list(self.tools_of(t.agent).values()),
                                              session=t.session, turn=t.turn)
                return step if step.by == agent.name else step.model_copy(update={"by": agent.name})
            except ContextOverflowError as e:
                if level >= len(TRIM_KEEP):
                    raise
                logger.warning("turn %s: %s — trimming older tool results", t.turn, str(e)[:200])
                t.memory["trim"] = level + 1
            except TransientModelError as e:
                if attempt >= self.retries:
                    raise
                attempt += 1
                delay = min(2.0 * 2 ** attempt, MAX_BACKOFF_S)
                logger.warning("turn %s: %s — retrying in %.0fs (%d/%d)", t.turn, e, delay, attempt, self.retries)
                if (notify := t.memory.get("on_retry")) is not None:
                    notify(attempt, self.retries, delay, str(e)[:200])
                await self.sleep(delay)

    async def _call(self, t: TurnState, call: ToolCall, base: dict, held: list[ToolCall]) -> list[Event]:
        tools = self.tools_of(t.agent)
        spec = tools.get(call.name)
        if spec is None:
            known = ", ".join(sorted(tools))
            return [ToolRan(**base, at=self.now(), by=t.agent, call=call, failed=True,
                            result=f"Error: there is no tool named {call.name!r}. Tools: {known}")]
        if call.name == TRANSFER:
            target = str(call.args.get("agent_name", ""))
            if target not in self.agents or target == t.agent:
                return [ToolRan(**base, at=self.now(), by=t.agent, call=call, failed=True,
                                result=f"Error: no agent {target!r} to hand over to.")]
            t.agent = target
            return [ToolRan(**base, at=self.now(), by=t.agent, call=call, result=f"Transferred to {target}.")]
        decision: Decision = Run()
        said: list[Event] = []
        for p in self.policies:
            opinion = await p.on_tool(t, call, spec)
            if opinion is None:
                continue
            decision = opinion
            if not isinstance(opinion, Run) or opinion.note:
                said.append(PolicySpoke(**base, at=self.now(), policy=p.name,
                                        said=f"{type(opinion).__name__.lower()} {call.name}"))
            break
        if isinstance(decision, Hold):
            if decision.queued:
                held.append(call)
            return [*said, ToolHeld(**base, at=self.now(), by=t.agent, call=call, reason=decision.reason)]
        t.calls += 1
        if isinstance(decision, Replace):
            return [*said, ToolRan(**base, at=self.now(), by=t.agent, call=call, result=decision.result,
                                   failed=decision.failed)]
        args = decision.args if decision.args is not None else call.args
        try:
            if inspect.iscoroutinefunction(spec.fn):
                out = await spec.fn(**args)
            else:
                out = await asyncio.to_thread(spec.fn, **args)
                if inspect.isawaitable(out):
                    out = await out
            failed = False
        except Exception as e:  # noqa: BLE001 — a tool's failure is the model's to read, not the turn's end
            out, failed = f"Error: {type(e).__name__}: {e}", True
        failed = failed or bool(failure_of(out))     # a tool that answers "Error: …" rather than raising
        for p in self.policies:
            out = p.on_result(t, call, spec, out)
        if decision.note:
            out = {"result": out, "instruction": decision.note}
        ran = call if args is call.args else call.model_copy(update={"args": dict(args)})
        return [*said, ToolRan(**base, at=self.now(), by=t.agent, call=ran, result=out, failed=failed)]

    async def _judge(self, t: TurnState, reply: str) -> tuple[Verdict, str]:
        for p in self.policies:
            said = await p.on_reply(t, reply)
            if said is not None and not isinstance(said, Accept):
                return said, p.name
        return Accept(), ""
