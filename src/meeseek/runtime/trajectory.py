"""One record per turn: what was asked, who answered, what it cost, what went
wrong on the way — and which version of the policy (runtime/policy.py) did it.

`summarize` reads a turn's events and derives it; the engine writes it to
.cache/learning/turns.jsonl after every turn (engine.py:_record) — the
console's, the Inbox's and the phone's, the MCP callers', the headless ones
— and not a replay's (runtime/replays.py). On the next message in the session
it also notes whether that message corrected or re-asked the turn
(runtime/feedback.py). The automatic signals are the harness's own marks,
found by their texts:

    tool_errors   a tool call that failed (core.ledger.failure_of)
    cap_hits      a call past its hard cap; grace: the turn's tool budget ran out
    consent_held  a write held for your yes;  inbox_held: held for the Inbox
    reconcile     a failed write the reply skipped (runtime/harness.py)
    appended      the 系統紀錄 note the harness had to add to the reply
    empty_nudge   the reply came back empty and was asked for again
    model_error   the turn ended on the model-error notice
    retries       the turn was resumed after a transient failure

A record older than RETAIN_DAYS is folded into a weekly line per skill and
policy (scoreboard.jsonl, runtime/reward.py) and dropped, so the store stays
the size of eight weeks of turns.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from meeseek.runtime.feedback import learning_dir
from meeseek.runtime.store import JsonlStore

logger = logging.getLogger(__name__)


def turns_store() -> JsonlStore:
    return JsonlStore(learning_dir() / "turns.jsonl")


RETAIN_DAYS = 56
_REQUEST_CHARS, _REPLY_CHARS = 500, 300


def _heads() -> dict[str, str]:
    """The fixed opening of each harness text a signal is found by."""
    from meeseek.context import prompts

    def head(name: str, n: int = 14) -> str:
        return prompts.source(name).split("{{")[0].strip()[:n]

    return {"cap_hits": head("runtime/callbacks#hard_cap"), "grace": head("runtime/callbacks#grace"),
            "time_grace": head("runtime/callbacks#time_grace"),
            "consent_held": head("runtime/consent#ask_first"), "inbox_held": head("runtime/inbox#held"),
            "empty_nudge": head("runtime/callbacks#empty_reply"), "model_error": head("runtime/callbacks#model_error"),
            "appended": "系統紀錄"}


def as_records(events: list) -> list[dict]:
    """Events in the shape ADK stores one (history.record's shape): ADK's own
    dumped, meeseek's engine's already in it."""
    return [e if isinstance(e, dict) else e.model_dump(mode="json", exclude_none=True) for e in events]


def _parts(e: dict) -> list[dict]:
    return (e.get("content") or {}).get("parts") or []


def _said(e: dict) -> str:
    return "".join(p.get("text") or "" for p in _parts(e) if not p.get("thought"))


def summarize(events: list, inv: str, *, root: str = "") -> dict:
    """The record of one invocation, from its events (pure: no I/O but prompt texts).
    ADK's events or the engine's records alike (as_records)."""
    from meeseek.core.ledger import failure_of
    from meeseek.runtime.usage import cost_usd

    heads = _heads()
    mine = [e for e in as_records(events) if e.get("invocation_id") == inv and not e.get("partial")]
    rec: dict[str, Any] = {"inv": inv, "calls": [], "agents": [], "models": {}, "request": "",
                           "auto": dict.fromkeys(["tool_errors", "cap_hits", "grace", "time_grace", "consent_held",
                                                  "inbox_held", "reconcile", "appended", "empty_nudge", "model_error"], 0),
                           "tokens": {"prompt": 0, "cached": 0, "output": 0}, "cost_usd": 0.0}
    reply, answered_by = "", ""
    for e in mine:
        author = e.get("author") or ""
        parts = _parts(e)
        if author == "user":
            t = "".join(p.get("text") or "" for p in parts)
            if t.strip().startswith(heads["empty_nudge"]):
                rec["auto"]["empty_nudge"] += 1
            elif t and not rec["request"]:
                rec["request"] = t.strip()[:_REQUEST_CHARS]
            continue
        if author and author not in rec["agents"]:
            rec["agents"].append(author)
        mv = e.get("model_version")
        if mv:
            rec["models"][author] = mv
        um = e.get("usage_metadata")
        if um:
            p_, c_ = um.get("prompt_token_count") or 0, um.get("cached_content_token_count") or 0
            o_ = (um.get("candidates_token_count") or 0) + (um.get("thoughts_token_count") or 0)
            rec["tokens"]["prompt"] += p_
            rec["tokens"]["cached"] += c_
            rec["tokens"]["output"] += o_
            rec["cost_usd"] += cost_usd(mv or "", p_, c_, o_) or 0.0
        for p in parts:
            fc, fr = p.get("function_call"), p.get("function_response")
            if fc:
                if fc.get("name") == "reconcile":
                    rec["auto"]["reconcile"] += 1
            elif fr:
                resp = fr.get("response")
                failed = bool(failure_of(resp))
                rec["calls"].append([fr.get("name"), failed])
                rec["auto"]["tool_errors"] += failed
                text = str(resp.get("result", "")) if isinstance(resp, dict) else str(resp)
                for k in ("cap_hits", "grace", "time_grace", "consent_held", "inbox_held"):
                    if heads[k] and text.startswith(heads[k]):
                        rec["auto"][k] += 1
        t = _said(e)
        if t:
            reply, answered_by = t, author
    if heads["appended"] in reply:
        rec["auto"]["appended"] = 1
    if reply.strip().startswith(heads["model_error"]):
        rec["auto"]["model_error"] = 1
    rec["reply_head"] = reply.strip()[:_REPLY_CHARS]
    rec["reply_chars"] = len(reply)
    skilled = [a for a in rec["agents"] if a != root and a.endswith("_agent")]
    rec["skill"] = (answered_by if answered_by in skilled else (skilled[-1] if skilled else "")) \
        .removesuffix("_agent") or "router"
    stamps = [e.get("timestamp") for e in mine if e.get("timestamp")]
    rec["at"] = min(stamps) if stamps else time.time()
    rec["latency_s"] = round(max(stamps) - min(stamps), 2) if len(stamps) > 1 else 0.0
    rec["cost_usd"] = round(rec["cost_usd"], 5)
    return rec


def _sha8(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:8]


def provenance(skill: str, agents: list[str]) -> dict:
    """Which policy answered: the bundle, the components of the agents that
    took part, the learned rules they were given, and the memory sections."""
    from meeseek.runtime import policy

    out: dict[str, Any] = {"bundle": policy.bundle_id(), "components": policy.components_of(agents)}
    try:
        from meeseek.runtime.learned_rules import parse_learned

        out["rules"] = {skill: [r.id for r in parse_learned(skill)]} if skill != "router" else {}
    except Exception:  # noqa: BLE001
        out["rules"] = {}
    try:
        from meeseek.context import memory, ontology, personal

        out["memory"] = {"pinned": _sha8(memory.load_pinned_notes()), "me": _sha8(personal.summary()),
                         "team": _sha8(ontology.patterns()), "facts": _sha8(memory.load_facts())}
    except Exception:  # noqa: BLE001
        out["memory"] = {}
    return out


def fold(records: list[dict]) -> list[dict]:
    """One record per invocation: a resumed turn writes again, the last wins."""
    last: dict[str, dict] = {}
    for r in records:
        last[r.get("inv", "")] = r
    return list(last.values())


def read(since: float = 0) -> list[dict]:
    return [r for r in fold(turns_store().read()) if r.get("at", 0) >= since]


def _previous(events: list, current_inv: str) -> tuple[str, str, float]:
    """(invocation, its request, when it ended) of the last turn before `current_inv`."""
    last_inv, last_end, request = "", 0.0, ""
    for e in as_records(events):
        inv = e.get("invocation_id")
        if not inv or inv == current_inv:
            continue
        if inv != last_inv:
            last_inv, request = inv, ""
        if e.get("author") == "user" and not request:
            request = _said(e).strip()
        last_end = e.get("timestamp") or last_end
    return last_inv, request, last_end
