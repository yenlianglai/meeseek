"""Run the harness eval: real agent, real reads, stubbed writes.

    uv run --extra dev python -m evals.harness.run --models default --runs 3
    uv run --extra dev python -m evals.harness.run --models lite --runs 3

`--models lite` rewrites every model tier to gemini-3.1-flash-lite in a
throwaway copy of project.yaml (PROJECT_CONFIG), so the weak-model run uses the
same code path. Results go to evals/harness/results/<label>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
LITE_MODEL = "gemini-3.1-flash-lite"
NO_HARNESS = False
DISABLED: set[str] = set()


def _prepare_config(models: str) -> None:
    """Point PROJECT_CONFIG at an eval copy with the chosen models."""
    cfg = yaml.safe_load((ROOT / "project.yaml").read_text(encoding="utf-8"))
    if models == "lite":
        for tier in ("router", "workflow", "fast", "coding"):
            cfg["models"][tier] = LITE_MODEL
    out = Path(tempfile.mkdtemp(prefix="harness-eval-")) / "project.yaml"
    out.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.environ["PROJECT_CONFIG"] = str(out)


# ---------------------------------------------------------------------------
# one run (write stubs: runtime/replays.py)
# ---------------------------------------------------------------------------

async def run_case(case: dict, run_no: int, sem: asyncio.Semaphore) -> dict:
    """One run of a case: the app's agents, tools and rules, in memory, writes
    stubbed. A case's injected fault fails its Nth model call as a dropped
    connection does; a disabled tool is answered "not available"; a pause
    stops the turn where the case says — right after a tool's result
    (`after_response`), or as the model asks for a call, before it runs
    (`on_call`) — and the turn is then resumed (TurnRunner.resume)."""
    from meeseek import engine
    from meeseek.adapters.memory import MemoryEventStore
    from meeseek.config import cfg
    from meeseek.core.events import ModelStepped, ToolHeld, ToolRan
    from meeseek.core.turn import BasePolicy, Replace, TransientModelError, TurnRunner
    from meeseek.runtime import orchestrator
    from meeseek.runtime.replays import _REPLAYING

    async with sem:
        executed: dict[str, int] = {}
        agents = await engine.with_mcp(engine.agents())
        fault = (case.get("fault") or {}).get("model_call")
        if fault:
            calls_so_far = [0]

            class Faulty:
                def __init__(self, inner):
                    self.inner = inner

                async def step(self, **kw):
                    calls_so_far[0] += 1
                    if calls_so_far[0] == fault:
                        raise TransientModelError("eval: injected dropped connection")
                    return await self.inner.step(**kw)

            for a in agents:
                a.model = Faulty(a.model)

        class Disabled(BasePolicy):
            name = "eval_disable"

            async def on_tool(self, t, call, spec):
                if call.name in DISABLED:
                    return Replace(f"{call.name} is not available in this run; do the work yourself.", failed=False)
                return None

        rules = engine.policies()
        if NO_HARNESS:   # the turn's budgets and caps only
            rules = [r for r in rules if r.name in ("call_budget", "clock", "arg_cap", "result_cap")]
        say = engine.say
        runner = TurnRunner(store=MemoryEventStore(), agents=agents, root=orchestrator.name(),
                            policies=engine.stubbed(rules, case.get("stubs") or {}, executed, Disabled()),
                            retries=cfg.models.transient_retries, sleep=_short_wait, say=say)
        token = _REPLAYING.set(True)
        turns, started = [], time.monotonic()
        try:
            for line in case["turns"]:
                retries, pauses = [0], [0]
                on_retry = lambda *a: retries.__setitem__(0, retries[0] + 1)  # noqa: E731
                rule = case.get("pause")

                def observe(e, rule=rule):
                    if rule and not pauses[0] and _pause_now(rule, e):
                        pauses[0] += 1
                        raise _Pause

                try:
                    try:
                        res = await runner.run("eval", line, by="eval", observe=observe, on_retry=on_retry)
                    except _Pause:
                        res = await runner.resume("eval", by="eval", on_retry=on_retry)
                    reply, turn = res.reply, res.turn
                    events = [e for e in runner.store.read("eval") if e.turn == turn]
                except Exception as e:  # a crashed run is a failed run, not a crashed eval
                    events, reply, turn = [], f"<<run error: {type(e).__name__}: {e}>>", None
                results = {e.call.id: (e.result if isinstance(e, ToolRan) else {"result": e.reason, "held": True})
                           for e in events if isinstance(e, (ToolRan, ToolHeld))}
                calls = [{"name": c.name, "id": c.id, "args": dict(c.args), "response": results.get(c.id)}
                         for e in events if isinstance(e, ModelStepped) for c in e.calls]
                turns.append({"user": line, "calls": calls, "reply": reply,
                              "model_events": sum(1 for e in events if isinstance(e, ModelStepped)),
                              "retries": retries[0], "pauses": pauses[0], "invocation": turn})
        finally:
            _REPLAYING.reset(token)
        return {"id": case["id"], "run": run_no, "turns": turns, "executed": executed,
                "seconds": round(time.monotonic() - started, 1)}


class _Pause(Exception):
    """The eval's stand-in for the console's pause button."""


def _pause_now(rule: dict, e) -> bool:
    from meeseek.core.events import ModelStepped, ToolRan

    if "after_response" in rule and isinstance(e, ToolRan) and e.call.name == rule["after_response"]:
        return True
    return "on_call" in rule and isinstance(e, ModelStepped) and any(c.name == rule["on_call"] for c in e.calls)


async def _short_wait(seconds: float) -> None:
    await asyncio.sleep(min(seconds, 1.0))  # a retry waits a second, not the backoff


def score(case: dict, result: dict) -> dict:
    exp = case.get("expect") or {}
    last = result["turns"][-1]
    # What the user sees: the reply, and any question card the turn put up.
    reply = "\n".join([last["reply"]] + [json.dumps(c["args"], ensure_ascii=False)
                                          for c in last["calls"] if c["name"] == "ask_user"])
    called = {c["name"] for t in result["turns"] for c in t["calls"]}
    exercised = not exp.get("writes") or bool(called & set(exp["writes"]))
    misses = []
    for group in exp.get("ack", []):
        if not re.search(group, reply):
            misses.append(f"ack:{group[:30]}")
    for pat in exp.get("forbid", []):
        if re.search(pat, reply):
            misses.append(f"forbid:{pat[:30]}")
    for s in exp.get("mention", []):
        if s not in reply:
            misses.append(f"mention:{s}")
    for tool in exp.get("once", []):  # a resumed turn must not redo a step
        ids = {c.get("id") or i for t in result["turns"] for i, c in enumerate(t["calls"]) if c["name"] == tool}
        if len(ids) > 1 or result.get("executed", {}).get(tool, 0) > 1:
            misses.append(f"once:{tool}")
    for tool in exp.get("read_before_rewrite", []):
        # after the harness held back a resumed write, a read must come before
        # the write is sent again
        calls = [c for t in result["turns"] for c in t["calls"]]
        held = next((i for i, c in enumerate(calls) if c["name"] == tool
                     and isinstance(c.get("response"), dict)
                     and c["response"].get("harness_status") == "unknown"), None)
        if held is None:
            misses.append(f"not_held:{tool}")
        else:
            again = next((i for i in range(held + 1, len(calls)) if calls[i]["name"] == tool), None)
            between = calls[held + 1:again] if again is not None else calls[held + 1:]
            from meeseek.runtime.harness import has_effect

            if again is not None and not any(not has_effect(c["name"]) and c["name"] != "transfer_to_agent"
                                             for c in between):
                misses.append(f"blind_rewrite:{tool}")
    if exp.get("transfer_to"):
        targets = {c["args"].get("agent_name") for t in result["turns"] for c in t["calls"]
                   if c["name"] == "transfer_to_agent"}
        if exp["transfer_to"] not in targets:
            misses.append(f"not_routed:{exp['transfer_to']}")
    for i, tools in (exp.get("forbid_on_turn") or {}).items():
        on_turn = {c["name"] for c in result["turns"][int(i)]["calls"]}
        if on_turn & set(tools):
            misses.append(f"turn{i}_called:{sorted(on_turn & set(tools))}")
    for i in exp.get("asks_on_turn", []):
        if not any(c["name"] == "ask_user" for c in result["turns"][i]["calls"]):
            misses.append(f"no_ask:turn{i}")
    for tool, arg in (exp.get("batch") or {}).items():
        # the first call carries several items; a later call re-sends only what
        # an earlier result reported FAILED (`file:line`: FAILED)
        calls_of = [c for t in result["turns"] for c in t["calls"] if c["name"] == tool]
        if not calls_of or len(calls_of[0]["args"].get(arg) or []) < 2:
            misses.append(f"not_batched:{tool}")
        for i, c in enumerate(calls_of[1:], 1):
            earlier = "\n".join(str(x.get("response")) for x in calls_of[:i])
            for item in c["args"].get(arg) or []:
                if f"`{item.get('file_path')}:{item.get('new_line')}`: FAILED" not in earlier:
                    misses.append(f"resent:{tool}")
    for tool, n in (exp.get("executed") or {}).items():
        # what the write stub actually ran — a call held back (consent, a
        # resumed write) is not a write
        if result.get("executed", {}).get(tool, 0) != n:
            misses.append(f"executed:{tool}={result.get('executed', {}).get(tool, 0)}")
    for tool in exp.get("calls", []):
        if tool not in called:
            misses.append(f"not_called:{tool}")
    for tool in exp.get("forbid_tools", []):
        if any(c["name"] == tool for t in result["turns"] for c in t["calls"]):
            misses.append(f"called:{tool}")
    if exp.get("same_turn"):
        i, j = exp["same_turn"]  # the answer resumed the turn that asked
        if result["turns"][i]["invocation"] != result["turns"][j]["invocation"]:
            misses.append(f"new_turn:{j}")
    if "<<run error" in reply:
        misses.append("run_error")
    return {"exercised": exercised, "passed": exercised and not misses, "misses": misses,
            "appended": "系統紀錄" in reply,
            "reconciles": sum(c["name"] == "reconcile" for t in result["turns"] for c in t["calls"]),
            "model_events": sum(t["model_events"] for t in result["turns"])}


async def main_async(args) -> int:
    spec = yaml.safe_load((HERE / "cases.yaml").read_text(encoding="utf-8"))
    cases = [c for c in spec["cases"] if not args.only or c["id"] in args.only]
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{args.label}.json"
    if args.rescore:  # re-grade saved replies against the current cases.yaml
        results = json.loads(out.read_text(encoding="utf-8"))
        args.runs = max(r["run"] for r in results) + 1
    else:
        sem = asyncio.Semaphore(args.concurrency)
        results = await asyncio.gather(*[run_case(c, r, sem) for c in cases for r in range(args.runs)])
    by_id = {c["id"]: c for c in cases}
    results = [r for r in results if r["id"] in by_id]
    for r in results:
        r["score"] = score(by_id[r["id"]], r)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"\n{args.label}  ({args.runs} runs per case)")
    print(f"{'case':34} pass  not-ex  reconcile  appended  model-calls  misses")
    total_p = total_ex = 0
    for c in cases:
        rs = [r for r in results if r["id"] == c["id"]]
        p = sum(r["score"]["passed"] for r in rs)
        ex = sum(r["score"]["exercised"] for r in rs)
        rec = sum(r["score"]["reconciles"] for r in rs)
        app_n = sum(r["score"].get("appended", False) for r in rs)
        mc = sum(r["score"]["model_events"] for r in rs) / len(rs)
        miss = sorted({m for r in rs for m in r["score"]["misses"]})
        total_p += p
        total_ex += ex
        print(f"{c['id']:34} {p}/{ex:<4} {len(rs) - ex:^6}  {rec:^9}  {app_n:^8}  {mc:^11.1f}  {'; '.join(miss)[:70]}")
    print(f"{'TOTAL':34} {total_p}/{total_ex}  (pass rate over exercised runs: "
          f"{total_p / total_ex:.0%})" if total_ex else "TOTAL: nothing exercised")
    print(f"→ {out.relative_to(ROOT)}")
    # Indexed with the policy it ran (runtime/cases.py): `--diff A B` compares two labels case by case.
    from meeseek.runtime import cases as library, policy

    per_case = {c["id"]: f"{sum(r['score']['passed'] for r in results if r['id'] == c['id'])}/"
                         f"{sum(r['score']['exercised'] for r in results if r['id'] == c['id'])}" for c in cases}
    library.record_run({"run_id": args.label, "kind": "harness", "label": args.label, "bundle": policy.bundle_id(),
                        "head": policy.manifest().get("head", ""), "cases": per_case,
                        "passed": total_p, "exercised": total_ex})
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", choices=["default", "lite"], default="default")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--label", default="")
    ap.add_argument("--rescore", action="store_true", help="re-grade results/<label>.json")
    ap.add_argument("--no-harness", action="store_true", help="run without the app's policies")
    ap.add_argument("--disable", nargs="*", default=[], help="tools answered with 'not available' (A/B)")
    ap.add_argument("--diff", nargs=2, metavar=("A", "B"), help="compare two runs' labels case by case, then exit")
    args = ap.parse_args()
    if args.diff:
        sys.path.insert(0, str(ROOT))
        from meeseek.runtime import cases as library

        changed = library.diff(*args.diff)
        for cid, ab in changed.items():
            print(f"{cid:34} {ab['a']} → {ab['b']}")
        print(f"{len(changed)} case(s) changed between {args.diff[0]} and {args.diff[1]}")
        return 0
    global NO_HARNESS, DISABLED
    NO_HARNESS = args.no_harness
    DISABLED = set(args.disable)
    args.label = args.label or args.models
    _prepare_config(args.models)
    sys.path.insert(0, str(ROOT))
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
