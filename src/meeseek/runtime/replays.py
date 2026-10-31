"""Replaying a conversation against the real agents without changing anything real.

Reads go to the real systems; every tool with an effect (`harness.has_effect`)
is answered by a stub in the shape the real tool answers with. The harness
eval stubs per-case outcomes; the weekly review replays a learned rule's
request with and without the rule; the weekly improvement replays cases
under a candidate policy. Both run on meeseek's engine (engine.py).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from meeseek.runtime.replaying import _REPLAYING, REPLAY_USERS, replaying  # noqa: F401 — one definition

def payload(tool: str, outcome: str, text: str) -> dict:
    """The shape the real tool answers with, by family: an MCP server's (a pack's
    or another's) — anything not in meeseek's own registry — or the registry's."""
    from meeseek.tools.registry import _INDEX

    if tool not in _INDEX:
        return {"content": [{"type": "text", "text": text}], "isError": outcome == "fail"}
    if outcome == "fail":  # a registry tool's exception, as on_tool_error_feedback renders it
        from meeseek.context import prompts

        return {"error": text, "tool_failed": True,
                "instruction": prompts.text("runtime/replay#tool_error")}
    return {"result": text}


async def stub_answer(name: str, args: dict, stubs: dict | None, executed: dict) -> dict | None:
    """What a stubbed tool answers: its rule from `stubs` (`{"ok"|"fail": text,
    "when": substring of the args, "delay": seconds}`, or a list of them), else
    a neutral success; None for a tool without an effect, which runs for real.
    `executed` counts the calls it answered."""
    from meeseek.runtime.harness import has_effect

    if not has_effect(name):
        return None
    executed[name] = executed.get(name, 0) + 1
    rules = (stubs or {}).get(name) or [{"ok": f"{name}: done (stub)"}]
    rules = rules if isinstance(rules, list) else [rules]
    text = json.dumps(args, ensure_ascii=False)
    for rule in rules:
        if "when" in rule and rule["when"] not in text:
            continue
        if rule.get("delay"):
            await asyncio.sleep(rule["delay"])
        outcome = "fail" if "fail" in rule else "ok"
        return payload(name, outcome, rule[outcome])
    return payload(name, "ok", f"{name}: done (stub)")


@dataclass(slots=True)
class Replay:
    reply: str = ""
    calls: list[dict] = field(default_factory=list)
    error: str = ""


@dataclass
class TurnRun:
    reply: str = ""
    calls: list = field(default_factory=list)       # [{name, args, failed}]


@dataclass
class SessionRun:
    turns: list = field(default_factory=list)       # one TurnRun per replayed message
    executed: dict = field(default_factory=dict)    # writes the stub answered
    cost_usd: float = 0.0
    seconds: float = 0.0
    error: str = ""


async def replay(prompt: str, *, exclude_rule: str = "") -> Replay:
    """`prompt` in a fresh session, writes stubbed; `exclude_rule` is a learned
    rule's id left out of the agents' memory."""
    from meeseek import engine

    return await engine.replay(prompt, exclude_rule=exclude_rule)


async def run_session(turns: list[str], *, prefix_events: list[dict] = (), stubs: dict | None = None,
                      bundle=None, timeout_s: float = 600) -> SessionRun:
    """A conversation replayed: its earlier events seeded as they were, then
    each of `turns` sent and answered afresh — under `bundle` (runtime/policy.py)
    when given."""
    from meeseek import engine

    return await engine.run_session(turns, prefix_events=prefix_events, stubs=stubs, bundle=bundle,
                                    timeout_s=timeout_s)
