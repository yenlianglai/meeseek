"""What the turns that really happened cost: model calls, tool calls, tool errors.

    uv run python -m evals.turn_cost                      # every turn in .cache/sessions.db
    uv run python -m evals.turn_cost --since 2026-10-01   # turns on or after a date
    uv run python -m evals.turn_cost --caller mcp         # callers whose user_id starts with "mcp"
    uv run python -m evals.turn_cost --json               # for comparing two periods

A turn is one invocation. A model call is a model event with content; a tool
error is a response the harness counts as failed, or a result that opens with an
error line. `repeats` counts turns that called one tool three or more times —
the sign of a parameter the model had to guess.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

_ERROR_LINE = re.compile(
    r"^(GitLab API error|Jira API error|Error|Diagram render failed|"
    r"\(sub-task failed\))")


def _is_error(response) -> bool:
    from meeseek.runtime.harness import _failure

    if _failure(response):
        return True
    result = response.get("result") if isinstance(response, dict) else None
    return isinstance(result, str) and bool(_ERROR_LINE.match(result.strip()))


def _caller_group(user_id: str) -> str:
    return "mcp" if user_id.startswith("mcp") else user_id


def load_turns(db: Path, since: str = "", caller: str = "") -> list[dict]:
    query = "select user_id, invocation_id, timestamp, event_data from events"
    args: list = []
    if since:
        query += " where timestamp >= ?"
        args.append(datetime.fromisoformat(since).timestamp())
    query += " order by timestamp"
    turns: dict[tuple, dict] = {}
    with sqlite3.connect(db) as conn:
        for user_id, inv, _, data in conn.execute(query, args):
            if caller and not user_id.startswith(caller):
                continue
            t = turns.setdefault((user_id, inv), {
                "caller": _caller_group(user_id), "model": collections.Counter(),
                "calls": [], "errors": collections.Counter()})
            event = json.loads(data)
            content = event.get("content") or {}
            parts = content.get("parts") or []
            if content.get("role") == "model" and parts and event.get("author") != "user":
                t["model"][event.get("author", "?")] += 1
            for p in parts:
                if p.get("function_call"):
                    t["calls"].append(p["function_call"]["name"])
                elif p.get("function_response") and _is_error(p["function_response"].get("response")):
                    t["errors"][p["function_response"]["name"]] += 1
    return list(turns.values())


def summarize(turns: list[dict]) -> dict:
    def spread(values: list[int]) -> dict:
        if not values:
            return {"turns": 0}
        values = sorted(values)
        return {"turns": len(values), "median": statistics.median(values),
                "mean": round(statistics.mean(values), 1),
                "p90": values[int(0.9 * (len(values) - 1))]}

    by_caller = collections.defaultdict(list)
    by_agent = collections.defaultdict(list)
    calls, errors, first, repeats = (collections.Counter() for _ in range(4))
    for t in turns:
        by_caller[t["caller"]].append(sum(t["model"].values()))
        for agent, n in t["model"].items():
            by_agent[agent].append(n)
        calls.update(t["calls"])
        errors.update(t["errors"])
        own = [c for c in t["calls"] if c != "transfer_to_agent"]
        if own:
            first[own[0]] += 1
        repeats.update(name for name, n in collections.Counter(own).items() if n >= 3)
    return {
        "model_calls_per_turn": spread([sum(t["model"].values()) for t in turns]),
        "by_caller": {k: spread(v) for k, v in sorted(by_caller.items(), key=lambda kv: -len(kv[1]))},
        "by_agent": {k: spread(v) for k, v in sorted(by_agent.items(), key=lambda kv: -sum(kv[1]))},
        "tools": {name: {"calls": n, "errors": errors[name], "repeats": repeats[name]}
                  for name, n in calls.most_common()},
        "first_tool": dict(first.most_common(12)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--since", default="")
    ap.add_argument("--caller", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.db is None:
        from meeseek.config.paths import home
        a.db = home().data / "sessions.db"
    s = summarize(load_turns(a.db, a.since, a.caller))
    if a.json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
        return 0

    def row(name: str, d: dict) -> str:
        if not d["turns"]:
            return f"  {name:24} {0:>6}"
        return f"  {name:24} {d['turns']:>6} {d['median']:>7.1f} {d['mean']:>6.1f} {d['p90']:>5}"

    head = f"  {'':24} {'turns':>6} {'median':>7} {'mean':>6} {'p90':>5}"
    print("model calls per turn\n" + head)
    print(row("all", s["model_calls_per_turn"]))
    for k, d in s["by_caller"].items():
        print(row(f"caller {k}", d))
    print("\nmodel calls per turn, by agent (turns it took part in)\n" + head)
    for k, d in s["by_agent"].items():
        print(row(k, d))
    print(f"\n  {'tool':24} {'calls':>6} {'errors':>7} {'err%':>5} {'repeats':>8}")
    for name, d in list(s["tools"].items())[:30]:
        pct = round(100 * d["errors"] / d["calls"]) if d["calls"] else 0
        print(f"  {name:24} {d['calls']:>6} {d['errors']:>7} {pct:>5} {d['repeats']:>8}")
    print("\nfirst tool of a turn: " + ", ".join(f"{k} {v}" for k, v in s["first_tool"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
