"""The case library: real turns that went badly or well, kept to replay a policy against.

A case is one turn from the conversation it happened in — the earlier events
as they were (`prefix_events`), the message that started it (`target_turn`),
and what came of it (`label`, `reference`: your 👎 note, the correction you
typed, the edit you made to a write before approving it). `mint` builds them
nightly from turns whose reward a person or a detector set (runtime/reward.py):

    negative   a 👎, a correction, a rejected or edited write — what a better
               policy must do better on
    positive   a 👍 or an approved write — what a better policy must not break

A rewording of a case is that case seen again (context/lifecycle.py `same`),
so the library holds situations, not repeats; it lives by the lifecycle (a case
seen once and never again retires after a month), keeps at most MAX_CASES of
each kind per skill, turns a negative it has seen solved twice into a positive,
and retires one whose replay errors twice — the systems it read have moved on.
`slice` picks what a week's budget can replay. Every replay run is indexed
(`record_run`) with its policy bundle and cost, so runs can be compared.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from meeseek.context import lifecycle
from meeseek.runtime.feedback import learning_dir
from meeseek.runtime.store import JsonlStore


def cases_dir() -> Path:
    return learning_dir() / "cases"


def runs_store() -> JsonlStore:
    return JsonlStore(learning_dir() / "runs.jsonl")


MAX_CASES = 12               # of each kind, per skill
MIN_REWARD, MIN_CONFIDENCE = 0.5, 0.6
PREFIX_MAX_BYTES = 80_000
_FIELD_MAX = 4_000           # a long tool result in a seeded event is cut to this
RUN_DAYS = 90


def _dir(skill: str) -> Path:
    return cases_dir() / skill


def load(skill: str) -> list[dict]:
    d = _dir(skill)
    out = []
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return out


def save(case: dict) -> None:
    d = _dir(case["skill"])
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{case['id']}.json").write_text(json.dumps(case, ensure_ascii=False, indent=1), encoding="utf-8")


def drop(case: dict) -> None:
    (_dir(case["skill"]) / f"{case['id']}.json").unlink(missing_ok=True)


def skills() -> list[str]:
    return sorted(p.name for p in cases_dir().iterdir() if p.is_dir()) if cases_dir().exists() else []


# --- building cases from turns -------------------------------------------------------------

def _cut(value, limit: int = _FIELD_MAX):
    """A seeded event, its long strings cut: a page of Jira need not replay whole."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…（截斷）"
    if isinstance(value, dict):
        return {k: _cut(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_cut(v, limit) for v in value]
    return value


def conversation(session_id: str, inv: str) -> tuple[list[dict], str]:
    """(the events before `inv` in its session, the text that started `inv`),
    read from the session store; the prefix kept under PREFIX_MAX_BYTES by
    dropping the oldest whole events first."""
    from meeseek.runtime import history

    prefix, target = [], ""
    for r in history.rows(session=session_id):
        row_inv, ev = r.turn, r.event
        if row_inv == inv:
            if not target and ev.get("author") == "user":
                target = "".join(p.get("text") or "" for p in (ev.get("content") or {}).get("parts") or [])
            continue
        if target:
            break                       # events after the turn are not its context
        prefix.append(_cut(ev))
    while prefix and len(json.dumps(prefix, ensure_ascii=False).encode()) > PREFIX_MAX_BYTES:
        prefix.pop(0)
    return prefix, target


def _reference(outcomes: list[dict]) -> str:
    """What the person wanted instead, in their words or edits."""
    bits = []
    for o in outcomes:
        if o.get("note"):
            bits.append(f"{o['kind']}：{o['note']}")
        if o.get("text") and o.get("kind") in ("correction", "reask"):
            bits.append(f"使用者接著說：{o['text']}")
        if o.get("diff"):
            bits.append(f"使用者把 {o.get('tool', '寫入')} 改成：{json.dumps(o['diff'], ensure_ascii=False)}")
        if o.get("kind") == "write_rejected":
            bits.append(f"使用者拒絕了 {o.get('tool', '寫入')}" + (f"：{o['note']}" if o.get("note") else ""))
    return "\n".join(dict.fromkeys(bits))[:2000]


def mint(days: float = 7) -> int:
    """New cases from the last `days` of graded turns; how many were added or seen again."""
    from meeseek.runtime import feedback, reward

    outs: dict[str, list[dict]] = {}
    for o in feedback.read(time.time() - (days + 1) * 86400):
        outs.setdefault(o["inv"], []).append(o)
    n = 0
    for turn, r in reward.rewards(days):
        if r.value is None or abs(r.value) < MIN_REWARD or r.confidence < MIN_CONFIDENCE or r.infra:
            continue
        kind = "negative" if r.value < 0 else "positive"
        n += add(turn, r, kind, outs.get(turn["inv"], []))
    return n


def add(turn: dict, r, kind: str, outcomes: list[dict]) -> int:
    """File one graded turn as a case, or count it against the case it repeats."""
    skill = turn.get("skill", "router")
    request = turn.get("request", "")
    if not request:
        return 0
    existing = load(skill)
    same = next((c for c in existing if c["kind"] == kind and c["status"] != "retired"
                 and lifecycle.same(c["request"], request)), None)
    now = time.time()
    if same:
        if turn["inv"] in [s["inv"] for s in same["src"]]:
            return 0
        same["src"] = (same["src"] + [{"session": turn.get("session", ""), "inv": turn["inv"]}])[-5:]
        same["seen"] += 1
        same["last_seen"] = now
        same["confirmed"] = same["confirmed"] or r.tier == "explicit"
        if (kind == "negative" and r.value < same["label"]["reward"]) or (kind == "positive" and r.value > same["label"]["reward"]):
            same["label"] = {"reward": r.value, "tier": r.tier, "tags": r.tags}
        same["reference"] = same.get("reference") or _reference(outcomes)
        same["status"] = lifecycle.status(confirmed=same["confirmed"], seen=same["seen"])
        save(same)
        return 1
    prefix, target = conversation(turn.get("session", ""), turn["inv"])
    if not target:
        return 0
    case = {"id": turn["inv"][:12].replace("-", "") or str(int(now)), "skill": skill, "kind": kind,
            "origin": "turn", "src": [{"session": turn.get("session", ""), "inv": turn["inv"]}],
            "created": now, "last_seen": now, "seen": 1, "confirmed": r.tier == "explicit",
            "request": request, "prefix_events": prefix, "target_turn": target,
            "label": {"reward": r.value, "tier": r.tier, "tags": r.tags},
            "reference": _reference(outcomes), "checks": {}, "reply_head": turn.get("reply_head", ""),
            "cost_hint_usd": max(0.02, turn.get("cost_usd", 0.065)), "results": []}
    case["status"] = lifecycle.status(confirmed=case["confirmed"], seen=1)
    save(case)
    evict(skill)
    return 1


def from_session(skill: str, events: list, invocation_id: str, *, origin: str, reference: str = "") -> dict | None:
    """A case from a live session (a learned rule's, runtime/learned_rules.py):
    the turns before the one that wrote the rule, with their last request as
    the target."""
    from meeseek.runtime.trajectory import as_records

    raw = [_cut(e) for e in as_records(events) if e.get("invocation_id") != invocation_id]
    users = [i for i, e in enumerate(raw) if e.get("author") == "user"]
    if not users:
        return None
    last = users[-1]
    target = "".join(p.get("text") or "" for p in (raw[last].get("content") or {}).get("parts") or [])
    prefix = raw[:last]
    while prefix and len(json.dumps(prefix, ensure_ascii=False).encode()) > PREFIX_MAX_BYTES:
        prefix.pop(0)
    now = time.time()
    case = {"id": origin.replace(":", "_")[:20], "skill": skill, "kind": "negative", "origin": origin,
            "src": [], "created": now, "last_seen": now, "seen": 1, "confirmed": True, "status": "confirmed",
            "request": target[:500], "prefix_events": prefix, "target_turn": target,
            "label": {"reward": -1.0, "tier": "explicit", "tags": []}, "reference": reference[:2000],
            "checks": {}, "reply_head": "", "cost_hint_usd": 0.065, "results": []}
    save(case)
    evict(skill)
    return case


# --- keeping it small ---------------------------------------------------------------------

_EVICT_ORDER = {"retired": 0, "guess": 1, "pattern": 2, "confirmed": 3}


def evict(skill: str) -> int:
    """Hold each kind to MAX_CASES: retired first, then guesses, then patterns,
    confirmed last — the oldest of each first."""
    gone = 0
    cases = load(skill)
    for kind in ("negative", "positive"):
        mine = sorted((c for c in cases if c["kind"] == kind),
                      key=lambda c: (_EVICT_ORDER.get(c["status"], 1), c["last_seen"]))
        for c in mine[:max(0, len(mine) - MAX_CASES)]:
            drop(c)
            gone += 1
    return gone


def retire(now: float | None = None) -> int:
    """Cases past their time (context/lifecycle.py) leave; how many."""
    now = now or time.time()
    gone = 0
    for skill in skills():
        for c in load(skill):
            if c["status"] == "retired" or lifecycle.expired(confirmed=c["confirmed"], seen=c["seen"],
                                                             last_seen=c["last_seen"], now=now):
                drop(c)
                gone += 1
    return gone


def record_result(case: dict, *, run: str, bundle: str, outcome: str) -> dict:
    """What a replay of the case under the current policy came to: "pass"
    (the base did well), "fail", or "error". A negative seen solved twice in a
    row becomes a positive; one that errors twice is retired as flaky."""
    case["results"] = (case.get("results", []) + [{"run": run, "bundle": bundle, "outcome": outcome,
                                                   "at": time.time()}])[-6:]
    last2 = [r["outcome"] for r in case["results"][-2:]]
    if last2 == ["error", "error"]:
        case["status"], case["retired_why"] = "retired", "flaky"
    elif case["kind"] == "negative" and last2 == ["pass", "pass"]:
        case["kind"], case["seen"], case["results"] = "positive", 1, []
    save(case)
    return case


def slice(skill: str, *, kind: str, n: int, budget_usd: float, ids: list[str] | None = None) -> list[dict]:
    """Up to `n` live cases of `kind` the budget can replay, cheapest first
    (or those named by `ids`)."""
    live = [c for c in load(skill) if c["kind"] == kind and c["status"] != "retired"
            and (ids is None or c["id"] in ids)]
    live.sort(key=lambda c: (c.get("cost_hint_usd", 0.065), -c["last_seen"]))
    out, spent = [], 0.0
    for c in live:
        if len(out) >= n or spent + c.get("cost_hint_usd", 0.065) > budget_usd:
            break
        out.append(c)
        spent += c.get("cost_hint_usd", 0.065)
    return out


# --- runs ---------------------------------------------------------------------------------

def record_run(run: dict) -> None:
    """Index one replay run: {run_id, kind, label, bundle, cases: {id: outcome}, passed, cost_usd, …}."""
    runs_store().append({**run, "finished": time.time()})


def runs(kind: str = "") -> list[dict]:
    return [r for r in runs_store().read() if not kind or r.get("kind") == kind]


def diff(a: str, b: str) -> dict:
    """Case by case, what changed between two runs."""
    ra = next((r for r in runs_store().read() if r.get("run_id") == a), {})
    rb = next((r for r in runs_store().read() if r.get("run_id") == b), {})
    ca, cb = ra.get("cases", {}), rb.get("cases", {})
    return {cid: {"a": ca.get(cid), "b": cb.get(cid)} for cid in sorted(set(ca) | set(cb)) if ca.get(cid) != cb.get(cid)}


def prune_runs(keep: set[str] = frozenset(), now: float | None = None) -> int:
    """Run records older than RUN_DAYS go, but for those a proposal names."""
    cutoff = (now or time.time()) - RUN_DAYS * 86400
    before = len(runs_store().read())
    runs_store().update(lambda rs: [r for r in rs if r.get("finished", 0) >= cutoff or r.get("run_id") in keep])
    return before - len(runs_store().read())
