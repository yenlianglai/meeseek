"""meeseek's own engine: the orchestrator and the skills as agents on core/turn.py.

    agents     the orchestrator (runtime/orchestrator.py) and one agent per
               skills/<name>/, each with its instruction, model tier and tools
    tools      registry tools by name, a skill's own tools.py, its ship tool,
               rules and facts, ask_user — every one a core ToolSpec
    policies   the Inbox's hold-and-approve, consent, the turn's budgets and
               clock, argument clamps, result caps, and the reply checks —
               worded by the project's prompt files
    store      the session's events, in .cache/meeseek.db under the home

`turn()` runs one.
"""

from __future__ import annotations

import inspect
import logging
from contextvars import ContextVar
from typing import Any, Callable, Sequence

from meeseek.adapters.llm import build_model
from meeseek.adapters.sqlite import SqliteEventStore
from meeseek.config import cfg
from meeseek.config.paths import home
from meeseek.context import prompts
from meeseek.core.events import ToolCall
from meeseek.core.policies import (
    CallBudget,
    Clock,
    Consent,
    EmptyReply,
    Evidence,
    FailedWriteCheck,
    PromiseCheck,
    ResumedWrites,
)
from meeseek.core.tools import Effect, ToolSpec, schema_of
from meeseek.core.turn import Agent, BasePolicy, Decision, Hold, Run, TurnResult, TurnRunner, TurnState

# The turn a tool runs in: {"user", "session_id"}, for the registry tools that
# ask for their origin (wants_origin).
_ORIGIN: ContextVar[dict] = ContextVar("meeseek_origin", default={})

# The project's wording for what a policy says (prompts/runtime/*.md), one
# literal call each so the prompt tests can hold every name and placeholder.
_SAY: dict[str, Callable[[dict], str]] = {
    "grace": lambda v: prompts.text("runtime/callbacks#grace", budget=v["budget"]),
    "hard_cap": lambda v: prompts.text("runtime/callbacks#hard_cap", tool=v["tool"], calls=v["calls"],
                                       budget=v["budget"]),
    "soft_cap": lambda v: prompts.text("runtime/callbacks#soft_cap", tool=v["tool"], calls=v["calls"]),
    "time_grace": lambda v: prompts.text("runtime/callbacks#time_grace", minutes=v["minutes"]),
    "held": lambda v: prompts.text("runtime/inbox#held", tool=v["tool"]),
    "declined": lambda v: prompts.text("runtime/inbox#rejected", tool=v["tool"], note=v.get("note", "")),
    "ask_first": lambda v: prompts.text("runtime/consent#ask_first", what=v["what"]),
    "failure_line": lambda v: prompts.text("runtime/harness#failure_line", tool=v["tool"], target=v["target"],
                                           detail=v["detail"]),
    "reconcile": lambda v: prompts.text("runtime/harness#reconcile", failures=v["failures"]),
    "appended_note": lambda v: prompts.text("runtime/harness#appended_note", failures=v["failures"]),
    "promise": lambda v: prompts.text("runtime/harness#promise", promise=v["promise"]),
    "promise_note": lambda v: prompts.text("runtime/harness#promise_note"),
    "empty_reply": lambda v: prompts.text("runtime/callbacks#empty_reply"),
    "resumed_write": lambda v: prompts.text("runtime/harness#resumed_write"),
    "split_the_call": lambda v: prompts.text("runtime/llm#split_the_call"),
    "dropped_result": lambda v: prompts.text("runtime/callbacks#dropped_result"),
}


def say(key: str, **values: Any) -> str:
    return _SAY[key](values)


# --- tools ------------------------------------------------------------------------

def _effect(name: str) -> Effect:
    from meeseek.runtime.harness import is_write

    return Effect.WRITE if is_write(name) else Effect.READ


def fn_spec(fn: Callable[..., Any], name: str = "") -> ToolSpec:
    """A plain function as a tool: its docstring and type hints are the schema."""
    from meeseek.core.tools import _split_doc

    name = name or fn.__name__
    params = schema_of(fn)
    params["properties"].pop("tool_context", None)          # filled in from the turn, never by the model
    takes_context = "tool_context" in inspect.signature(fn).parameters
    run = fn
    if takes_context:
        def run(**kw: Any) -> Any:  # noqa: E306 — what the tool reads of its turn
            return fn(tool_context=_tool_context(), **kw)
    return ToolSpec(name=name, description=_split_doc(fn.__doc__ or "")[0], fn=run,
                    parameters=params, effect=_effect(name))


def _tool_context():
    """What a tool taking `tool_context` reads — the turn's id and its
    session's events (in stored shape) — from the turn it runs in."""
    from types import SimpleNamespace

    from meeseek.core.turn import current
    from meeseek.runtime import history

    t = current()
    if t is None:
        return None
    events = [r for e in t.all_events if (r := history.record(e)) is not None]
    return SimpleNamespace(invocation_id=t.turn, session=SimpleNamespace(id=t.session, user_id=t.by, events=events))


def _turn_images() -> list[tuple[str, bytes]]:
    """The pictures that came with the message this turn answers: (mime, bytes)."""
    from meeseek.adapters.blobs import blobs
    from meeseek.core.events import UserSaid
    from meeseek.core.turn import current

    t = current()
    said = next((e for e in reversed(t.events) if isinstance(e, UserSaid)), None) if t else None
    return [(a.mime, blobs().get(a.ref)) for a in (said.attachments if said else ())
            if a.ref and a.mime.startswith("image/")]


def registry_spec(name: str) -> ToolSpec:
    """A registry tool, handed its turn's origin when it asks for it."""
    from meeseek.tools.registry import _INDEX, RAW_TOOLS

    td = _INDEX[name]  # KeyError for an unknown name: a typo fails at startup
    fn = td.fn
    if td.wants_origin:
        def fn(**kw: Any) -> Any:  # noqa: E306
            return td.fn(_origin=dict(_ORIGIN.get()), **kw)
    elif td.wants_user_images:
        def fn(**kw: Any) -> Any:  # noqa: E306 — "use the pictures I sent" reaches the tool as its references
            return td.fn(_attached_images=_turn_images(), **kw)
    return ToolSpec(name=td.name, description=td.description, fn=fn, parameters=td.parameters,
                    effect=Effect.WRITE if td.writes else Effect.READ, shows_raw=name in RAW_TOOLS)


def _ask_user() -> ToolSpec:
    from meeseek.runtime.ask import ask_user

    return fn_spec(ask_user)


def _reads(media: bool) -> list[ToolSpec]:
    from meeseek.tools.registry import read_tool_names

    return [registry_spec(n) for n in read_tool_names(media=media)]


# --- agents -----------------------------------------------------------------------

def _context(agent: str | None, t: TurnState) -> str:
    """What every turn adds after the agent's own instruction: memory, open
    tasks, and the workstream the conversation is about."""
    from meeseek.context.memory import memory_instruction
    from meeseek.runtime import tasks, workstreams

    return "\n\n".join(s for s in (memory_instruction(agent), tasks.instruction(session_id=t.session, user_id=t.by),
                                    workstreams.instruction(t.session)) if s)


class _Bundled:
    """A model that is shown the tool and skill descriptions as the active
    policy bundle has them (runtime/policy.py) — a replay under a candidate
    policy; as written, otherwise. Never a tool with an effect."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def step(self, *, system, history, tools, session, turn):
        import dataclasses

        from meeseek.runtime import policy

        b = policy.active()
        if b and (b.tool_descriptions or b.descriptions):
            from meeseek.runtime.harness import has_effect
            from meeseek.runtime.skills import discover_skills

            tools = [dataclasses.replace(t, description=b.tool_descriptions[t.name])
                     if t.name in b.tool_descriptions and not has_effect(t.name) else t for t in tools]
            for s in discover_skills():
                if s.name in b.descriptions and s.description and s.description in system:
                    system = system.replace(s.description, b.descriptions[s.name])
        return await self.inner.step(system=system, history=history, tools=tools, session=session, turn=turn)


def _model(model_id: str, by: str):
    return _Bundled(build_model(model_id, by=by))


def router() -> Agent:
    from meeseek.runtime import orchestrator

    from meeseek.tools.web_search import web_search

    # The orchestrator holds no read tools — the skill it hands over to has them —
    # but a one-line lookup on the web or in its past conversations is not worth a hand-off.
    tools = [*(fn_spec(f) for f in orchestrator.own_tools()),
             *(registry_spec(n) for n in orchestrator.registry_tools()), fn_spec(web_search), _ask_user()]
    return Agent(name=orchestrator.name(), model=_model(cfg.models.router, orchestrator.name()),
                 system=lambda t: f"{orchestrator.instruction()}\n\n{_context(None, t)}".strip(), tools=tools,
                 description=_routes())


def _routes() -> str:
    from meeseek.runtime.skills import discover_skills

    return "routes each request to the skill that should answer it: " + "; ".join(
        s.description for s in discover_skills())


def skill_agent(skill) -> Agent:
    from meeseek.context.memory import remember_fact
    from meeseek.context.prompts import render
    from meeseek.runtime import policy
    from meeseek.runtime.learned_rules import make_propose_tool
    from meeseek.runtime.skills import _BUILTIN_TOOLS, _load_local_tools, _make_ship_tool, common
    from meeseek.tools.registry import _INDEX, GENERAL_TOOLS
    from meeseek.tools.web_search import web_search

    local = _load_local_tools(skill)
    tools = [*_reads(media=True), fn_spec(web_search), *(fn_spec(f) for f in local.values())]
    for n in skill.tools:
        if n in local or n in _BUILTIN_TOOLS or n in GENERAL_TOOLS or n not in _INDEX:
            continue   # not the registry's: a pack serves it (with_mcp)
        tools.append(registry_spec(n))
    if skill.ship_categories:
        tools.append(fn_spec(_make_ship_tool(skill)))
    tools += [fn_spec(make_propose_tool(skill.name)), fn_spec(remember_fact), _ask_user()]

    def system(t: TurnState, s=skill) -> str:
        sop = render(policy.skill_body(s.name, s.path, s.body)) + common()
        return f"{sop}\n\n{_context(s.name, t)}".strip()

    return Agent(name=skill.agent_name, model=_model(cfg.models.resolve(skill.model), skill.agent_name),
                 system=system, tools=_unique(tools), description=skill.description)


def _unique(tools: list[ToolSpec]) -> list[ToolSpec]:
    """One declaration per name: Gemini rejects two (the later wins, as MCP over a registry twin)."""
    return list({t.name: t for t in tools}.values())


def agents() -> list[Agent]:
    from meeseek.runtime.skills import discover_skills

    return [router(), *(skill_agent(s) for s in discover_skills())]


# --- policies -----------------------------------------------------------------------

class InboxGate(BasePolicy):
    """In an Inbox turn every write waits for the person, exactly as called;
    the call they approved runs, and what it returned is kept on the item."""

    name = "inbox"

    async def on_tool(self, t: TurnState, call: ToolCall, spec: ToolSpec) -> Decision | None:
        from meeseek.runtime import inbox

        if not spec.writes or t.by != inbox.INBOX_USER:
            return None
        item = inbox.by_session(t.session) or {"id": "", "actions": [], "session_id": t.session}
        if item["id"] and inbox.take_approval(item, call.name, dict(call.args)):
            t.memory["inbox_approved"] = call.name     # your yes is the consent the consent policy asks for
            return Run()
        if item["id"]:
            inbox.hold(item, call.name, dict(call.args), t.agent, t.turn)
        return Hold(say("held", tool=call.name), queued=bool(item["id"]))

    def on_result(self, t: TurnState, call: ToolCall, spec: ToolSpec, result: Any) -> Any:
        from meeseek.runtime import inbox

        if spec.writes and t.memory.get("inbox_approved") == call.name:
            item = inbox.by_session(t.session)
            if item:
                inbox.settle(item, call.name, result if isinstance(result, dict) else {"result": result})
        return result


class ArgCap(BasePolicy):
    """A skill's `limits:` clamp row and line counts at the query: fetching fewer
    beats truncating a huge result after (callbacks.make_arg_cap)."""

    name = "arg_cap"

    def __init__(self, caps: Callable[[TurnState], dict[str, dict]]) -> None:
        self.caps = caps

    async def on_tool(self, t: TurnState, call: ToolCall, spec: ToolSpec) -> Decision | None:
        tool_caps = self.caps(t).get(call.name)
        if not tool_caps:
            return None
        args = dict(call.args)
        for param, max_val in tool_caps.items():
            max_n, current = int(max_val), args.get(param)
            if current is None:
                args[param] = max_val
                continue
            try:
                n = int(current)
            except (TypeError, ValueError):
                n = max_n + 1
            if n > max_n:
                args[param] = str(max_n) if isinstance(current, str) else max_n
        return None if args == call.args else Run(args=args)


class ResultCap(BasePolicy):
    """What a result costs every later step: MCP's duplicated payloads trimmed,
    anything past the cap cut to its head (callbacks.after_tool_callback)."""

    name = "result_cap"

    def on_result(self, t: TurnState, call: ToolCall, spec: ToolSpec, result: Any) -> Any:
        from meeseek.runtime.results import cap_result, slim_mcp

        result = slim_mcp(result) or result
        return cap_result(result) or result


def _per_agent(field: str) -> Callable[[TurnState], dict]:
    from meeseek.runtime.media import media_call_limits
    from meeseek.runtime.skills import discover_skills

    by_agent = {s.agent_name: getattr(s, field) for s in discover_skills()}
    base = media_call_limits() if field == "call_limits" else {}
    return lambda t: {**base, **by_agent.get(t.agent, {})}


def policies() -> list[BasePolicy]:
    from meeseek.runtime import harness, inbox, wakes
    from meeseek.runtime.consent import CONSENT_TOOLS
    from meeseek.tools.registry import RAW_TOOLS

    def backed(session: str) -> bool:
        if wakes.pending(session):
            return True
        item = inbox.by_session(session)
        return bool(item and any(a["status"] in ("pending", "approved", "running") for a in item.get("actions", [])))

    return [ResumedWrites(harness.is_write, say=say),
            InboxGate(),
            Consent(CONSENT_TOOLS, approved=lambda t, c: t.memory.get("inbox_approved") == c.name, say=say),
            CallBudget(cfg.models.iteration_budget, per_tool=_per_agent("call_limits"), say=say),
            Clock(cfg.models.turn_minutes, say=say),
            ArgCap(_per_agent("tool_limits")),
            ResultCap(),
            EmptyReply(say=say),
            FailedWriteCheck(harness.is_write, say=say),
            PromiseCheck(backed, say=say),
            Evidence(lambda name: name in RAW_TOOLS or name in _pack_raw())]


def _pack_raw() -> set[str]:
    from meeseek.app import current

    a = current()
    return a.mcp_tools.raw_tools() if a is not None and a.mcp_tools is not None else set()


# --- long conversations ------------------------------------------------------------

def _chars(events: list) -> int:
    import json

    from meeseek.core.turn import conversation

    return sum(len(m.text) + sum(len(json.dumps(c.args, ensure_ascii=False)) for c in m.calls)
               for m in conversation(events))


def _transcript(events: list, max_chars: int = 120_000) -> str:
    import json

    from meeseek.core.turn import conversation

    lines = []
    for m in conversation(events):
        if m.text:
            lines.append(f"[{m.role}] {m.text.strip()}"[:2000] if m.role == "tool" else f"[{m.role}] {m.text.strip()}")
        for c in m.calls:
            lines.append(f"[tool call] {c.name}({json.dumps(c.args, ensure_ascii=False, default=str)[:200]})")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars] + "\n…(transcript truncated)"


async def summarize(transcript: str, previous: str) -> str:
    """One call to the fast tier: the conversation so far, as working memory."""
    from meeseek.core.events import UserSaid

    parts = [prompts.text("runtime/callbacks#compaction_prompt")]
    if previous:
        parts.append(prompts.text("runtime/callbacks#compaction_previous", summary=previous))
    parts.append(prompts.text("runtime/callbacks#compaction_transcript", transcript=transcript))
    step = await build_model(cfg.models.fast, by="compaction").step(
        system="", history=[UserSaid(session="compaction", turn="c", text="\n\n".join(parts))], tools=[],
        session="compaction", turn="c")
    return step.text.strip()


def compactor(threshold: int, keep: int, summarize: Callable | None = None) -> Callable:
    """Past `threshold` chars, everything before the last `keep` events — cut
    where a message of the person's begins, so no call loses its result — is
    shown as one summary. A later one folds the earlier summary in."""
    from meeseek.core.events import Summarized, UserSaid

    async def compact(t: TurnState):
        events = t.all_events
        if threshold <= 0 or _chars(events) <= threshold:
            return None
        prev = next((e for e in reversed(events) if isinstance(e, Summarized)), None)
        start = prev.upto if prev else 0
        cut = next((i for i in range(len(events) - keep, start, -1) if isinstance(events[i], UserSaid)), None)
        if not cut:
            return None
        covered = [e for e in events[start:cut] if not isinstance(e, Summarized)]
        text = await (summarize or globals()["summarize"])(_transcript(covered), prev.summary if prev else "")
        if not text:
            return None
        head = prompts.text("runtime/callbacks#compaction_head", summary=text)
        return Summarized(session=t.session, turn=t.turn, by="compaction", summary=head, upto=cut)

    return compact


# --- running a turn -------------------------------------------------------------------

def store() -> SqliteEventStore:
    return SqliteEventStore(home().data / "meeseek.db")


async def with_mcp(built: list[Agent]) -> list[Agent]:
    """Each skill's MCP tools added: a server's that names the skill (project.yaml
    `mcp_servers[].agents`, for a skill with `mcp: true`), and a pack's for every
    skill — its reads, and the writes the skill's `tools:` grant. A registry
    tool of the same name gives way to its MCP twin."""
    from meeseek.adapters.mcp import McpTools
    from meeseek.app import app
    from meeseek.runtime.skills import discover_skills
    from meeseek.tools.registry import _INDEX

    if not cfg.mcp_servers:
        return built
    a = app()
    a.mcp_tools = a.mcp_tools or McpTools()
    by_agent = {s.agent_name: s for s in discover_skills()}
    for agent in built:
        skill = by_agent.get(agent.name)
        if skill is None:
            continue
        agent.description = skill.description      # now with the packs' placeholders
        served = await a.mcp_tools.tools_for(skill.name, tuple(skill.tools), named=skill.use_mcp)
        agent.tools = _unique([*agent.tools, *served])
        have = {t.name for t in agent.tools}
        missing = [n for n in skill.tools if n not in have and n not in _INDEX]
        if missing:
            logging.getLogger(__name__).warning("skill %s: no running server has %s", skill.name, missing)
    for agent in built:
        if agent.name not in by_agent:
            agent.description = _routes()
    return built


async def runner() -> TurnRunner:
    from meeseek.runtime import orchestrator

    return TurnRunner(store=store(), agents=await with_mcp(agents()), root=orchestrator.name(),
                      policies=policies(), retries=cfg.models.transient_retries, say=say,
                      compactor=compactor(cfg.models.compaction_chars, cfg.models.compaction_keep))


async def _slash(session: str, text: str, by: str, observe: Callable | None) -> tuple[TurnResult | None, str]:
    """A slash command: one that runs here answers without a model (the result,
    or None and the agent to start with for one that goes to a skill)."""
    import asyncio
    import time
    import uuid

    from meeseek.core.events import Replied, UserSaid
    from meeseek.runtime import orchestrator
    from meeseek.runtime.slash_commands import _BY_NAME, _SLASH_RE

    m = _SLASH_RE.match(text.strip())
    cmd = _BY_NAME.get(m.group(1).lower()) if m else None
    if cmd is None:
        return None, ""
    if cmd.skill:
        return None, cmd.target
    turn_id = uuid.uuid4().hex[:12]
    said = UserSaid(session=session, turn=turn_id, by=by, text=text)
    s = store()
    s.append([said])
    output = await asyncio.to_thread(cmd.run)
    reply = Replied(session=session, turn=turn_id, by=orchestrator.name(), text=output, at=time.time())
    s.append([reply])
    if observe is not None:
        observe(said)
        observe(reply)
    return TurnResult(reply=output, turn=turn_id, held=[], events=[said, reply]), ""


# --- what a turn teaches (runtime/trajectory.py, runtime/feedback.py) ---------------------

def _note_followup(session: str, text: str, by: str) -> None:
    """Before the message joins the session: does it correct or re-ask the last turn?"""
    import time

    from meeseek.runtime import feedback, history, trajectory
    from meeseek.runtime.past_sessions import typed
    from meeseek.runtime.replaying import replaying

    if replaying(by):
        return
    try:
        found = history.rows(session=session)
        said = typed(by, text) or ""
        prev_inv, prev_req, prev_end = trajectory._previous([r.event for r in found], "")
        if not prev_inv or not said:
            return
        kind = feedback.implicit(said, prev_request=typed(by, prev_req) or prev_req,
                                 gap_s=time.time() - (prev_end or time.time()),
                                 waiting=history.pending_question(found) is not None)
        if kind:
            feedback.record(prev_inv, session, kind, by="detector", text=said[:300])
    except Exception:  # noqa: BLE001 — a signal lost, never a turn
        logging.getLogger(__name__).warning("engine: implicit signal failed", exc_info=True)


def _record(session: str, result: TurnResult, by: str) -> None:
    """The turn's record for the learning loop (runtime/trajectory.py)."""
    from meeseek.core.events import PolicySpoke
    from meeseek.runtime import history, orchestrator, trajectory
    from meeseek.runtime.replaying import replaying

    if replaying(by):
        return
    try:
        records = [r for e in result.events if (r := history.record(e)) is not None]
        rec = trajectory.summarize(records, result.turn, root=orchestrator.name())
        rec["auto"]["reconcile"] += sum(1 for e in result.events if isinstance(e, PolicySpoke) and e.to_model
                                        and e.policy == "failed_writes")
        spoke = [e.policy for e in result.events if isinstance(e, PolicySpoke)]
        rec.update(session=session, user=by, retries=0, flags={p: spoke.count(p) for p in dict.fromkeys(spoke)},
                   policy=trajectory.provenance(rec["skill"], rec["agents"]))
        trajectory.turns_store().append(rec)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("engine: could not record a turn", exc_info=True)


async def turn(session: str, text: str, *, by: str = "webui", watched: bool = True,
               observe: Callable | None = None, attachments: Sequence[tuple[str, bytes]] = (),
               on_retry: Callable | None = None) -> TurnResult:
    """One turn on meeseek's own engine. `attachments` are (mime type, bytes):
    images and PDFs that came with the message."""
    from meeseek.adapters.blobs import blobs
    from meeseek.core.events import Attachment

    token = _ORIGIN.set({"user": by, "session_id": session})
    try:
        _carry_over(session, by)
        files = [Attachment(mime=mime, ref=blobs().put(data)) for mime, data in attachments]
        _note_followup(session, text, by)
        _workstream(session, text, by)
        done, start_with = await _slash(session, text, by, observe) if not files else (None, "")
        if done is None:
            done = await (await runner()).run(session, text, watched=watched, by=by, observe=observe,
                                              start_with=start_with, attachments=files, on_retry=on_retry)
        _record(session, done, by)
        _workstream_after(session, text, done, by)
        return done
    finally:
        _ORIGIN.reset(token)


def _workstream(session: str, text: str, by: str) -> None:
    """Attach the conversation to the piece of work its message names, so this
    turn and the next carry what is known about it (runtime/workstreams.py)."""
    from meeseek.runtime import workstreams
    from meeseek.runtime.replaying import replaying

    if replaying(by):
        return
    try:
        workstreams.on_message(session, text, by)
    except Exception:  # noqa: BLE001 — a turn never fails for its workstream
        logging.getLogger(__name__).warning("engine: workstream lookup failed", exc_info=True)


def _workstream_after(session: str, text: str, done: TurnResult, by: str) -> None:
    from meeseek.runtime import workstreams
    from meeseek.runtime.replaying import replaying

    if replaying(by):
        return
    try:
        workstreams.after_turn(session, text, done.reply, by)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("engine: workstream update failed", exc_info=True)


def _carry_over(session: str, by: str) -> None:
    """A conversation from before the engine switch, continued: its events are
    copied into the store first, so the agent sees what was said."""
    from meeseek.runtime import history

    if store().read(session):
        return
    if old := history.legacy_events(session, by):
        store().append(old)


async def resume(session: str, *, by: str = "", watched: bool = True, observe: Callable | None = None,
                 on_retry: Callable | None = None) -> TurnResult:
    """Carry on the session's last turn where it stopped — a restart, a dropped
    connection, a pause — instead of asking it again (TurnRunner.resume).
    Raises NothingToResume when its last turn finished."""
    token = _ORIGIN.set({"user": by, "session_id": session})
    try:
        done = await (await runner()).resume(session, by=by, watched=watched, observe=observe, on_retry=on_retry)
        _record(session, done, by or "webui")
        return done
    finally:
        _ORIGIN.reset(token)


def run_sync(job: Callable[[], Any]) -> Any:
    """Run an async job from synchronous code, on an event loop of its own (in
    a thread of its own when this one already runs a loop)."""
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(job())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(job())).result()


class Packs:
    """The MCP servers' tools for one job off the main loop (the morning plan):
    started on first use in this job's loop, closed with it."""

    def __init__(self) -> None:
        from meeseek.adapters.mcp import McpTools

        self.mcp = McpTools() if cfg.mcp_servers else None

    async def reads(self) -> list[ToolSpec]:
        """The read tools of the servers for every skill (packs)."""
        if self.mcp is None:
            return []
        tools = await self.mcp.tools_for("", named=False)
        effects = self.mcp.effects()
        return [t for t in tools if effects.get(t.name, "read") == "read"]

    async def call(self, name: str, args: dict) -> str:
        """A tool by name — the registry's, else a server's — answered as text."""
        import asyncio
        import json

        from meeseek.tools.registry import _INDEX

        if name in _INDEX:
            spec = registry_spec(name)
            return str(await asyncio.to_thread(lambda: spec.fn(**args)))
        if self.mcp is None:
            raise LookupError(f"no tool named {name!r}: it is not meeseek's and no MCP server is configured")
        out = await self.mcp.call(name, args)
        text = "\n".join(c.get("text", "") for c in out.get("content", []) if c.get("type") == "text")
        if out.get("isError"):
            raise RuntimeError(text or json.dumps(out, ensure_ascii=False)[:300])
        return text

    async def close(self) -> None:
        if self.mcp is not None:
            await self.mcp.close()


async def headless(system_prompt: str, user_message: str, *, agent_name: str, model: str, max_turns: int,
                   memory: bool = True, packs: Packs | None = None) -> str:
    """One turn with no person on the other end: the registry's read tools and
    the packs', without pictures."""
    import uuid

    from meeseek.context.memory import memory_instruction
    from meeseek.runtime.sessions import WORKFLOW_USER

    own = packs is None
    packs = packs or Packs()
    try:
        extra = memory_instruction(None) if memory else ""
        tools = _unique([*_reads(media=False), *await packs.reads()])
        agent = Agent(name=agent_name, model=build_model(cfg.models.resolve(model), by=agent_name),
                      system=lambda t: f"{system_prompt}\n\n{extra}".strip(), tools=tools)
        runner = TurnRunner(store=store(), agents=[agent],
                            policies=[CallBudget(max_turns * 2, say=say), ResultCap()],
                            retries=cfg.models.transient_retries)
        result = await runner.run(f"workflow.{uuid.uuid4().hex[:6]}", user_message, watched=False, by=WORKFLOW_USER)
        return result.reply.strip()
    finally:
        if own:
            await packs.close()


def headless_turn(system_prompt: str, user_message: str, *, agent_name: str, model: str, max_turns: int,
                  memory: bool) -> str:
    """One turn with no person on the other end — the morning plan: read tools
    only, without pictures. Synchronous; from inside a running loop (a tool
    called mid-turn) it gets a thread of its own."""
    return run_sync(lambda: headless(system_prompt, user_message, agent_name=agent_name, model=model,
                                     max_turns=max_turns, memory=memory))


# --- replays (runtime/replays.py) ---------------------------------------------------

def stubbed(rules: list[BasePolicy], stubs: dict | None, executed: dict, *extra: BasePolicy) -> list[BasePolicy]:
    """`rules` with every effect stubbed (StubEffects), then `extra`. The stub
    goes first, so no other policy's opinion can let an effect through — after
    ResumedWrites alone, which only ever holds a call back, so that a write an
    interruption left outcome-unknown is held back, not counted as sent."""
    first = [r for r in rules if r.name == "resumed_writes"]
    return [*first, StubEffects(stubs, executed), *extra, *(r for r in rules if r.name != "resumed_writes")]


class StubEffects(BasePolicy):
    """In a replay, nothing with an effect runs: it is answered in the real
    tool's shape (runtime/replays.stub_answer). First in line, so no other
    policy's opinion can let one through."""

    name = "stub_effects"

    def __init__(self, stubs: dict | None, executed: dict) -> None:
        self.stubs, self.executed = stubs, executed

    async def on_tool(self, t: TurnState, call: ToolCall, spec: ToolSpec) -> Decision | None:
        from meeseek.core.turn import Replace
        from meeseek.runtime.replays import stub_answer

        answer = await stub_answer(call.name, dict(call.args), self.stubs, self.executed)
        if answer is None:
            return None
        from meeseek.core.ledger import failure_of

        return Replace(answer, failed=bool(failure_of(answer)))


async def run_session(turns: list[str], *, prefix_events=(), stubs: dict | None = None, bundle=None,
                      timeout_s: float = 600):
    """runtime/replays.run_session on this engine: in memory, effects stubbed."""
    import asyncio
    import time
    import uuid

    from meeseek.adapters.memory import MemoryEventStore
    from meeseek.core.events import ModelStepped, ToolRan
    from meeseek.runtime import history, orchestrator, policy
    from meeseek.runtime.replays import _REPLAYING, SessionRun, TurnRun
    from meeseek.runtime.usage import cost_usd

    out = SessionRun()
    mem, sid = MemoryEventStore(), f"replay-{uuid.uuid4().hex[:8]}"
    seeded = history.to_core(list(prefix_events), sid)
    mem.append(seeded)
    token = _REPLAYING.set(True)
    started = time.monotonic()
    try:
        with policy.using(bundle):
            replayer = TurnRunner(store=mem, agents=await with_mcp(agents()), root=orchestrator.name(),
                                  policies=stubbed(policies(), stubs, out.executed), say=say,
                                  retries=cfg.models.transient_retries)
            for text in turns:
                res = await asyncio.wait_for(replayer.run(sid, text, by="replay"), timeout=timeout_s)
                out.turns.append(TurnRun(reply=res.reply, calls=[
                    {"name": e.call.name, "args": dict(e.call.args), "failed": e.failed}
                    for e in res.events if isinstance(e, ToolRan) and e.call.name != "transfer_to_agent"]))
    except Exception as e:  # noqa: BLE001
        out.error = f"{type(e).__name__}: {e}"[:300]
    finally:
        _REPLAYING.reset(token)
        out.seconds = round(time.monotonic() - started, 1)
        for e in mem.read(sid)[len(seeded):]:
            if isinstance(e, ModelStepped) and e.usage is not None:
                out.cost_usd += cost_usd(e.usage.model, e.usage.input_tokens, e.usage.cached_tokens,
                                         e.usage.output_tokens) or 0.0
        out.cost_usd = round(out.cost_usd, 5)
    return out


async def replay(prompt: str, *, exclude_rule: str = ""):
    """runtime/replays.replay on this engine."""
    from meeseek.runtime.learned_rules import excluding
    from meeseek.runtime.replays import Replay

    with excluding(exclude_rule):
        run = await run_session([prompt])
    turn = run.turns[0] if run.turns else None
    return Replay(reply=turn.reply if turn else "", calls=[{"name": c["name"], "args": c["args"]} for c in
                                                           (turn.calls if turn else [])], error=run.error)
