"""The rules a turn runs under, one Policy each.

    on a tool call   CallBudget · Clock · ApprovalGate · Consent · ResumedWrites
    on the reply     FailedWriteCheck · PromiseCheck · Evidence

Each takes what it needs to know about the world as plain callables (is this
a write, is anything scheduled for this session, hold this call for the
person), so it is tested with lambdas and wired by the app. What a policy
says to the model or the person is a `Say`: English by default, the
project's prompt files in the app (prompts/runtime/*.md).
"""

from __future__ import annotations

import json
import time
from typing import Callable, Protocol

from meeseek.core import ledger
from meeseek.core.events import ModelStepped, ToolCall, ToolRan
from meeseek.core.tools import ToolSpec
from meeseek.core.turn import (
    RUNNER_SAY,
    Annotate,
    BasePolicy,
    Decision,
    Hold,
    Replace,
    Run,
    SendBack,
    TurnState,
    Verdict,
    is_unknown,
    unknown_outcome,
)

DEFAULT_SAY = {
    "grace": "The tool budget for this turn is spent ({budget} calls). Do not call tools again: answer now from "
             "what you have, say what is done and what is not, and where the next turn should pick up.",
    "hard_cap": "{tool} was called {calls} times this turn (advised: {budget}). It did not run: you are looping. "
                "Answer from what you have.",
    "soft_cap": "You have called {tool} {calls} times this turn. Converge: answer from what you have unless one "
                "more call is clearly needed.",
    "time_grace": "This unattended turn has run past its time limit ({minutes} minutes). Do not call tools again: "
                  "answer now from what you have, and say what is still open.",
    "held": "Not run yet: nobody is here to approve a write in this unattended turn, so it waits for the person "
            "as action {action} — exactly this call, run if they approve. Say in your reply what you queued and why.",
    "declined": "The person declined this write.{note}",
    "ask_first": "Not written: {what} changes every later turn, so it waits for the person's yes. Ask them first "
                 "(ask_user), then call this again.",
    "failure_line": "- {tool} ({target}): {detail}",
    "reconcile": "[system check] These writes failed and your reply does not say so:\n{failures}\nRewrite the "
                 "reply: what succeeded, what failed and why, and the next step. Do not call the failed part done.",
    "appended_note": "⚠️ System record — these writes did not succeed:\n{failures}",
    "promise": "[system check] Your reply promises a later step ({promise}), but nothing is scheduled to wake you. "
               "Either schedule it now, or rewrite the reply to say what is true now.",
    "promise_note": "⚠️ System record — the follow-up above is not scheduled; ask again when you need it.",
    "empty_reply": "[system] Your last step ended without any reply text; the person saw nothing. Answer now from "
                   "what you found above; if it is not enough, say what you looked at and what is missing. Do not "
                   "repeat a lookup already made.",
    **RUNNER_SAY,
}


class Say(Protocol):
    def __call__(self, key: str, **kw) -> str: ...


def default_say(key: str, **kw) -> str:
    return DEFAULT_SAY[key].format(**kw)


class CallBudget(BasePolicy):
    """At most `calls` tool calls a turn; a tool past its own advised count is
    told to converge, and past twice that it is blocked as a loop."""

    name = "call_budget"

    def __init__(self, calls: int, per_tool: dict[str, int] | Callable[[TurnState], dict[str, int]] | None = None,
                 say: Say = default_say, hard_multiplier: int = 2) -> None:
        self.calls, self.say, self.hard = calls, say, hard_multiplier
        self.per_tool = per_tool if callable(per_tool) else (lambda t, caps=dict(per_tool or {}): caps)

    async def on_tool(self, t: TurnState, call: ToolCall, spec: ToolSpec) -> Decision | None:
        if self.calls and t.calls >= self.calls:
            return Replace(self.say("grace", budget=self.calls))
        cap = self.per_tool(t).get(call.name)
        if not cap:
            return None
        counts = t.memory.setdefault(self.name, {})
        used = counts.get(call.name, 0)
        if used >= cap * self.hard:
            return Replace(self.say("hard_cap", tool=call.name, calls=used, budget=cap))
        counts[call.name] = used + 1
        if used + 1 > cap:
            return Run(note=self.say("soft_cap", tool=call.name, calls=used + 1))
        return None


class Clock(BasePolicy):
    """A turn nobody is watching gets `minutes` of wall clock; a person in the
    console stops their own turn when they have had enough."""

    name = "clock"

    def __init__(self, minutes: float, now: Callable[[], float] = time.time, say: Say = default_say) -> None:
        self.minutes, self.now, self.say = minutes, now, say

    async def on_tool(self, t: TurnState, call: ToolCall, spec: ToolSpec) -> Decision | None:
        if t.watched or not self.minutes or self.now() - t.started <= self.minutes * 60:
            return None
        return Replace(self.say("time_grace", minutes=int(self.minutes)))


class Approver(Protocol):
    async def approve(self, t: TurnState, call: ToolCall, spec: ToolSpec) -> tuple[bool, str]: ...


class Holder(Protocol):
    def hold(self, t: TurnState, call: ToolCall, spec: ToolSpec) -> str:
        """Queue the call for the person to decide later; its action id."""


class ApprovalGate(BasePolicy):
    """A write nobody is watching waits for the person, exactly as called. A
    watched write runs once `approver` says yes (with no approver, the person
    watching is the approval)."""

    name = "approval"

    def __init__(self, holder: Holder | None, approver: Approver | None = None, say: Say = default_say) -> None:
        self.holder, self.approver, self.say = holder, approver, say

    async def on_tool(self, t: TurnState, call: ToolCall, spec: ToolSpec) -> Decision | None:
        if not spec.writes:
            return None
        if not t.watched:
            if self.holder is None:
                return Hold("Not run: nobody is here to approve a write in this unattended turn, "
                            "and there is no queue to hold it.")
            return Hold(self.say("held", tool=call.name, action=self.holder.hold(t, call, spec)), queued=True)
        if self.approver is None:
            return None
        ok, note = await self.approver.approve(t, call, spec)
        return None if ok else Hold(self.say("declined", tool=call.name, note=f" They said: {note}" if note else ""))


class Consent(BasePolicy):
    """A write that changes every later turn (a learned rule, a remembered
    fact) needs the person's yes in the conversation, or an approval."""

    name = "consent"

    def __init__(self, tools: dict[str, str], approved: Callable[[TurnState, ToolCall], bool] = lambda t, c: False,
                 say: Say = default_say) -> None:
        self.tools, self.approved, self.say = tools, approved, say

    async def on_tool(self, t: TurnState, call: ToolCall, spec: ToolSpec) -> Decision | None:
        what = self.tools.get(call.name)
        if what is None or self.approved(t, call) or ledger.consented(t.all_events, t.turn):
            return None
        return Hold(self.say("ask_first", what=what))


def _asked(t: TurnState) -> str:
    """What this turn already put to the person through ask_user."""
    return "\n".join(json.dumps(c.args, ensure_ascii=False) for e in t.events if isinstance(e, ModelStepped)
                     for c in e.calls if c.name == "ask_user")


class EmptyReply(BasePolicy):
    """A turn that ends with no text left the person with nothing: sent back
    once to answer from what it found."""

    name = "empty_reply"

    def __init__(self, say: Say = default_say) -> None:
        self.say = say

    async def on_reply(self, t: TurnState, reply: str) -> Verdict | None:
        if reply.strip() or t.sent_back.get(self.name):
            return None
        return SendBack(self.say("empty_reply"))


class ResumedWrites(BasePolicy):
    """A write an interruption left outcome-unknown is not sent again until a
    read since then has checked whether it took effect: sent twice, a ticket is
    filed twice."""

    name = "resumed_writes"

    def __init__(self, writes: Callable[[str], bool], say: Say = default_say) -> None:
        self.writes, self.say = writes, say

    async def on_tool(self, t: TurnState, call: ToolCall, spec: ToolSpec) -> Decision | None:
        if not spec.writes:
            return None
        ran = [e for e in t.events if isinstance(e, ToolRan)]
        unknown = max((i for i, e in enumerate(ran) if e.call.name == call.name and is_unknown(e.result)), default=None)
        if unknown is None or any(not self.writes(e.call.name) for e in ran[unknown + 1:]):
            return None
        return Replace(result=unknown_outcome(self.say("resumed_write")), failed=False)


class FailedWriteCheck(BasePolicy):
    """A reply may not leave out a write that failed — this turn's, or the last
    two turns' when it talks about their target. Sent back once to be
    rewritten; the second time it ships with the facts appended."""

    name = "failed_writes"

    def __init__(self, writes: Callable[[str], bool], target_of: Callable[[str], tuple[str, ...]] = lambda n: (),
                 say: Say = default_say) -> None:
        self.writes, self.target_of, self.say = writes, target_of, say

    def _describe(self, entries: list[ledger.Entry]) -> str:
        return "\n".join(self.say("failure_line", tool=e.tool, target=e.target, detail=e.detail) for e in entries)

    async def on_reply(self, t: TurnState, reply: str) -> Verdict | None:
        now = ledger.ledger(t.events, t.turn, self.writes, self.target_of)
        earlier = [e for turn in ledger.earlier_turns(t.history, t.turn)
                   for e in ledger.ledger(t.history, turn, self.writes, self.target_of)]
        missing = ledger.unreported(now, reply + "\n" + _asked(t), earlier)
        if not missing:
            return None
        listing = self._describe(missing)
        if not t.sent_back.get(self.name):
            return SendBack(self.say("reconcile", failures=listing))
        return Annotate(self.say("appended_note", failures=listing))


class PromiseCheck(BasePolicy):
    """A reply that promises a later step needs something that will make it
    happen: a wake on this session, a write waiting for approval, or a
    question this turn asked."""

    name = "promises"

    def __init__(self, backed: Callable[[str], bool], say: Say = default_say) -> None:
        self.backed, self.say = backed, say

    async def on_reply(self, t: TurnState, reply: str) -> Verdict | None:
        p = ledger.promise(reply)
        if not p or _asked(t) or self.backed(t.session):
            return None
        if not t.sent_back.get(self.name):
            return SendBack(self.say("promise", promise=p))
        return Annotate(self.say("promise_note"))


class Evidence(BasePolicy):
    """Raw output the person asked to see (log lines, a pod table) reaches the
    reply: if a turn read some and the reply shows no code block, the last
    block goes first."""

    name = "evidence"

    def __init__(self, shows_raw: Callable[[str], bool]) -> None:
        self.shows_raw = shows_raw

    async def on_reply(self, t: TurnState, reply: str) -> Verdict | None:
        if "```" in reply or not reply.strip():
            return None
        block = ledger.raw_block(t.events, t.turn, self.shows_raw)
        return Annotate(block, before=True) if block else None
