"""Does every invariant in the core have a test that fails without it?

    uv run python scripts/mutation_check.py            # exit 1 and the survivors on a gap

Each mutant breaks one rule the turn runner, a policy or a one-shot model call
keeps — a budget off by one, a check skipped, a repair dropped, a file not
sent — and the acceptance tests (tests/unit/test_turn_runner.py,
test_core.py, test_oneshot.py) must fail on it. A mutant that survives is a
rule nothing checks. The source is restored after each, whatever happens.
Curated rather than generated (mutmut): each one is a way the rule could
actually break, so a survivor is worth reading.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "src" / "meeseek" / "core"
TESTS = ["tests/unit/test_turn_runner.py", "tests/unit/test_core.py", "tests/unit/test_oneshot.py"]

MUTANTS: list[tuple[str, str, str, str]] = [
    # (what breaks, file, original, mutated)
    ("the call budget is off by one", "policies.py",
     "if self.calls and t.calls >= self.calls:", "if self.calls and t.calls > self.calls:"),
    ("a looping tool is never blocked", "policies.py",
     "if used >= cap * self.hard:", "if False:"),
    ("a tool past its advice is not told to converge", "policies.py",
     "if used + 1 > cap:", "if False:"),
    ("a watched turn gets a clock too", "policies.py",
     "if t.watched or not self.minutes", "if not self.minutes"),
    ("the clock never runs out", "policies.py",
     "self.now() - t.started <= self.minutes * 60", "True"),
    ("an unwatched write runs instead of waiting", "policies.py",
     "        if not t.watched:\n            if self.holder is None:", "        if False:\n            if self.holder is None:"),
    ("a watched write runs without the approver", "policies.py",
     "ok, note = await self.approver.approve(t, call, spec)", "ok, note = True, ''"),
    ("a rule is written without consent", "policies.py",
     "if what is None or self.approved(t, call) or ledger.consented(t.all_events, t.turn):", "if True:"),
    ("a hidden failure is never sent back", "policies.py",
     "            return SendBack(self.say(\"reconcile\", failures=listing))", "            return None"),
    ("a hidden failure is sent back forever", "policies.py",
     "        if not t.sent_back.get(self.name):\n            return SendBack(self.say(\"reconcile\"",
     "        if True:\n            return SendBack(self.say(\"reconcile\""),
    ("an earlier turn's failure is forgotten", "policies.py",
     "earlier = [e for turn in ledger.earlier_turns(t.history, t.turn)", "earlier = [e for turn in []"),
    ("a promise is never checked", "policies.py",
     "if not p or _asked(t) or self.backed(t.session):", "if True:"),
    ("a scheduled promise is still refused", "policies.py",
     "if not p or _asked(t) or self.backed(t.session):", "if not p or _asked(t):"),
    ("raw evidence is dropped", "policies.py",
     "return Annotate(block, before=True) if block else None", "return None"),
    ("an earlier success settles a later failure", "ledger.py",
     "if any(x.status == \"ok\" and (x.tool, x.target) == key for x in entries[i + 1:]):",
     "if any(x.status == \"ok\" and (x.tool, x.target) == key for x in entries):"),
    ("a later success no longer settles a failure", "ledger.py",
     "if any(x.status == \"ok\" and (x.tool, x.target) == key for x in entries[i + 1:]):", "if False:"),
    ("saying it failed is not trusted", "ledger.py",
     "    if FAILURE_TERMS.search(reply):\n        return []\n    failures", "    failures"),
    ("a failed write reads as done", "ledger.py",
     "entry.status, entry.detail = (\"failed\", detail) if detail else (\"ok\", \"\")",
     "entry.status, entry.detail = (\"ok\", \"\")"),
    ("a refusal reads as a yes", "ledger.py",
     "return not REFUSAL.search(answer.strip())", "return True"),
    ("a cut-off call is left without a result", "turn.py",
     "            out.append(Message(role=\"tool\", text=UNKNOWN_OUTCOME, call_id=c.id, name=c.name))",
     "            pass"),
    ("a tool result loses its call", "turn.py",
     "out.append(Message(role=\"tool\", text=text, call_id=e.call.id, name=e.call.name))",
     "out.append(Message(role=\"tool\", text=text, call_id=\"\", name=e.call.name))"),
    ("a sent-back reply is never counted", "turn.py",
     "t.sent_back[by_policy] = t.sent_back.get(by_policy, 0) + 1", "pass"),
    ("a transient model error ends the turn", "turn.py",
     "                if attempt >= self.retries:\n                    raise", "                raise"),
    ("a held call runs anyway", "turn.py",
     "        if isinstance(decision, Hold):\n            if decision.queued:",
     "        if False:\n            if decision.queued:"),
    ("a failing tool ends the turn", "turn.py",
     "        except Exception as e:  # noqa: BLE001 — a tool's failure is the model's to read, not the turn's end",
     "        except ZeroDivisionError as e:"),
    ("the session is not read back", "turn.py",
     "        history = self.store.read(session)\n", "        history = []\n"),
    ("a handover does not change who answers", "turn.py",
     "            t.agent = target\n", "            pass\n"),
    ("the next turn starts at the root again", "turn.py",
     "                return e.by if e.by in self.agents else self.root", "                return self.root"),
    ("an agent sees every agent's tools", "turn.py",
     "        specs = {t.name: t for t in self.agents[agent].tools}",
     "        specs = {t.name: t for a in self.agents.values() for t in a.tools}"),
    ("a clamp is ignored", "turn.py",
     "        args = decision.args if decision.args is not None else call.args", "        args = call.args"),
    ("a result is not shaped", "turn.py",
     "            out = p.on_result(t, call, spec, out)", "            pass"),
    ("a resumed turn sends its in-flight write again", "turn.py",
     "                if spec is not None and spec.writes:", "                if False:"),
    ("a write left outcome-unknown is sent again blindly", "policies.py",
     "        return Replace(result=unknown_outcome(self.say(\"resumed_write\")), failed=False)", "        return None"),
    ("a read since never lets the write go", "policies.py",
     "any(not self.writes(e.call.name) for e in ran[unknown + 1:])", "False"),
    ("an overflowing request is not trimmed", "turn.py",
     "                t.memory[\"trim\"] = level + 1", "                raise"),
    ("trimming keeps every result whole", "turn.py",
     "    old = set(ran[:-keep] if keep else ran)", "    old = set()"),
    ("a call cut off by the output limit is run", "turn.py",
     "            if step.calls and (step.cut_off or any(\"_unparsed\" in c.args for c in step.calls)):",
     "            if False:"),
    # one-shot model calls (src/meeseek/runtime/oneshot.py, named from core/)
    ("a one-shot call ignores the configured model", "../runtime/oneshot.py",
     "m = build_model(model, by=source)", "m = build_model(\"gemini-2.5-flash\", by=source)"),
    ("a one-shot call drops its files", "../runtime/oneshot.py",
     "attachments=tuple(Attachment(mime=mime, ref=ref) for (mime, _), ref in zip(files, refs))", "attachments=()"),
    ("a one-shot call keeps its files in memory", "../runtime/oneshot.py",
     "            release(ref)", "            pass"),
    ("a one-shot transient failure is not asked again", "../runtime/oneshot.py",
     "RETRIES = 1", "RETRIES = 0"),
]


def _tests_pass() -> bool:
    res = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS],
                         cwd=ROOT, capture_output=True, text=True)
    return res.returncode == 0


def main() -> int:
    if not _tests_pass():
        print("the tests fail before any mutation: fix them first")
        return 2
    survivors = []
    for what, name, original, mutated in MUTANTS:
        path = CORE / name
        source = path.read_text(encoding="utf-8")
        if source.count(original) != 1:
            print(f"? {what}: the original is not in {name} exactly once — update the mutant")
            survivors.append(what)
            continue
        try:
            path.write_text(source.replace(original, mutated), encoding="utf-8")
            caught = not _tests_pass()
        finally:
            path.write_text(source, encoding="utf-8")
        print(("caught    " if caught else "SURVIVED  ") + what)
        if not caught:
            survivors.append(what)
    print(f"\n{len(MUTANTS) - len(survivors)}/{len(MUTANTS)} caught")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
