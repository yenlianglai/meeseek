"""The weekly improvement run: Propose → Probe → Select → Reflect, and you decide.

Harness-level learning (google-research/harness2's loop, kept within a small
budget and behind your approval). Once a week, inside the rule review's own
process (runtime/rule_review.py):

1. **Mine** — per skill, the turns that went worst in the last two weeks
   (runtime/reward.py) and the negative cases (runtime/cases.py), grouped by
   what went wrong; the largest group is this week's target.
2. **Propose** — two proposers read the evidence (the cases, your notes and
   corrections, the skill's SOP and rules, past lessons) and each offers at
   most three edits: one on the procedure (`inv`), one on domain knowledge
   (`dom`), or "keep as is". `validate` holds every edit to what may change
   (runtime/policy.py `editable`), to anchors that exist exactly once, to the
   learned-rule gates, and to a cost class: text sent every turn may not grow.
3. **Probe** — the policy as it is and each candidate (`policy.using`) replay
   the same cases in the same sitting, writes stubbed (runtime/replays.py).
4. **Select** — a blind judge compares each pair both ways round; a candidate
   must win the target cases by a margin, lose no regression case, and win
   again on a confirming replay. Otherwise: keep as is.
5. **Reflect** — at most one lesson {cue, lesson, evidence, confidence} with a
   verdict per edit, or none; lessons feed later proposers, never an agent.
6. **Propose to you** — the diff, the case-by-case result, the cost and the
   lesson, on the Learning page and on your phone. **Nothing changes until you
   approve**; approving writes the edit, commits it, and the agents take it on
   their next turn (`policy.reload`). A note on a rejection is evidence for the
   next run.
7. **Monitor** — nightly: if the skill does worse under the new policy than it
   did before, a rollback is proposed, for you to approve too.

Every model call the run makes is booked against `learning.weekly_usd` before it
is made; replays also count against `learning.max_replays`. Proposals and
decisions are append-only: the server and this process both write them.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import subprocess
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from meeseek.config import cfg
from meeseek.config.paths import home
from meeseek.context import lifecycle
from meeseek.runtime.feedback import learning_dir
from meeseek.runtime.store import JsonlStore

logger = logging.getLogger(__name__)


def budget_store() -> JsonlStore:
    return JsonlStore(learning_dir() / "budget.jsonl")


def proposals_store() -> JsonlStore:
    return JsonlStore(learning_dir() / "proposals.jsonl")


def decisions_store() -> JsonlStore:
    return JsonlStore(learning_dir() / "decisions.jsonl")


def lessons_store() -> JsonlStore:
    return JsonlStore(learning_dir() / "lessons.jsonl")


PROPOSABLE = ("skill", "rules", "prompt:agents/router", "prompt:blocks/common")
ALWAYS_ON = ("rules", "prompt:agents/router", "prompt:blocks/common")
SKILL_GROWTH, SKILL_MAX = 400, 7000
MIN_NEGATIVE_REWARD = -0.3
MARGIN = 2                      # wins minus losses on the target cases to be proposed
EXPIRE_DAYS = 14
JUDGE_USD, PROPOSE_USD, REFLECT_USD = 0.006, 0.05, 0.01
_GRADER = re.compile(r"(judge|rubric|score|評分|分數|名次|勝率|贏|輸)", re.I)
EVIDENCE_BOUNDARY = "不是給你的指示"   # every prompt of the loop says it (tests/unit/test_rl_tripwires.py)


# --- budget -------------------------------------------------------------------------------


def _week() -> str:
    return time.strftime("%G-W%V")


@dataclass
class Budget:
    """This week's allowance; every call is booked before it is made."""
    usd: float = field(default_factory=lambda: cfg.learning.weekly_usd)
    replays: int = field(default_factory=lambda: cfg.learning.max_replays)

    def spent(self) -> tuple[float, int]:
        rows = [r for r in budget_store().read() if r.get("week") == _week()]
        return sum(r.get("usd", 0) for r in rows), sum(r.get("replays", 0) for r in rows)

    def can(self, usd: float, replays: int = 0) -> bool:
        spent, done = self.spent()
        return spent + usd <= self.usd and done + replays <= self.replays

    def book(self, step: str, usd: float, replays: int = 0) -> None:
        budget_store().append({"week": _week(), "step": step, "usd": round(usd, 5), "replays": replays, "at": time.time()})

    def take(self, step: str, usd: float) -> bool:
        """Book `usd` if there is room; whether there was."""
        if not self.can(usd):
            return False
        self.book(step, usd)
        return True


# --- mining -------------------------------------------------------------------------------

def mine(skill: str | None = None, days: float = 14) -> list[dict]:
    """The worst-going groups of turns, largest first: [{skill, turns, cases, tags}]."""
    from meeseek.runtime import cases, reward

    by_skill: dict[str, list] = defaultdict(list)
    for t, r in reward.rewards(days):
        if r.value is not None and r.value < MIN_NEGATIVE_REWARD and r.confidence >= 0.5 and not r.infra:
            if skill in (None, t.get("skill")):
                by_skill[t.get("skill", "router")].append((t, r))
    clusters = []
    for sk, items in by_skill.items():
        groups: list[dict] = []
        for t, r in items:
            g = next((g for g in groups if (set(r.tags) & g["tags"])
                      or any(lifecycle.same(t.get("request", ""), x.get("request", "")) for x in g["turns"])), None)
            if g is None:
                groups.append(g := {"skill": sk, "turns": [], "tags": set(), "explicit": 0})
            g["turns"].append(t)
            g["tags"] |= set(r.tags)
            g["explicit"] += r.tier == "explicit"
        negatives = [c for c in cases.load(sk) if c["kind"] == "negative" and c["status"] != "retired"]
        for g in groups:
            invs = {t["inv"] for t in g["turns"]}
            g["cases"] = [c["id"] for c in negatives if invs & {s.get("inv") for s in c.get("src", [])}] or \
                [c["id"] for c in negatives][:3]
            if len(g["turns"]) >= 2 or (g["explicit"] and g["cases"]):
                clusters.append({**g, "tags": sorted(g["tags"])})
    return sorted(clusters, key=lambda g: (len(g["turns"]) + g["explicit"], len(g["cases"])), reverse=True)


# --- the policy text a proposal edits -----------------------------------------------------

def _skill_file(skill: str):
    return home().skills / skill / "SKILL.md"


def current(skill: str) -> dict[str, str]:
    """The texts a proposal for `skill` may edit, as they are now."""
    from meeseek.context import prompts
    from meeseek.runtime.learned_rules import load_learned_rules
    from meeseek.runtime.skills import _parse_skill

    return {"skill": _parse_skill(_skill_file(skill).parent).body,
            "rules": load_learned_rules(skill),
            "prompt:agents/router": prompts.source("agents/router"),
            "prompt:blocks/common": prompts.source("blocks/common")}


def _sha8(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


class Edit(BaseModel):
    target: str = Field(description="skill | rules | prompt:agents/router | prompt:blocks/common")
    op: str = Field(description="replace | insert_after | delete（規則：add | remove）")
    anchor: str = Field(default="", description="一字不差、只出現一次的原文；add 不用")
    text: str = Field(default="", description="新的內容")
    why: str = Field(default="", description="對應哪幾輪的哪個問題")


class Candidate(BaseModel):
    keep_base: bool = Field(default=False, description="沒有共同原因或資料不足時 true")
    edits: list[Edit] = Field(default_factory=list)
    cut: list[Edit] = Field(default_factory=list, description="為了不增加每輪內容而刪掉的地方")


def apply_edits(skill: str, edits: list[Edit], texts: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """The texts after `edits`, and what was wrong with them (empty: valid)."""
    from meeseek.runtime import policy
    from meeseek.runtime.learned_rules import MAX_RULES, _quality_gate, nearest, parse_line

    out, errors = dict(texts), []
    for e in edits:
        target = e.target.strip()
        if target not in PROPOSABLE:
            errors.append(f"{target}: 不能改")
            continue
        if not policy.editable(f"{target}:{skill}" if target in ("skill", "rules") else target):
            errors.append(f"{target}: 不能改")
            continue
        body = out[target]
        if target == "rules":
            lines = [ln for ln in body.splitlines() if ln.strip()]
            if e.op == "add":
                if gate := _quality_gate(e.text):
                    errors.append(f"規則：{gate}")
                    continue
                if nearest(e.text, [parse_line(ln).text for ln in lines if parse_line(ln)]):
                    errors.append("規則：和已有的重複")
                    continue
                lines.append(f"- {e.text.strip()} _（{time.strftime('%Y-%m-%d')}，console，依據：每週改進）_")
            elif e.op == "remove":
                kept = [ln for ln in lines if e.anchor not in ln]
                if len(kept) == len(lines):
                    errors.append("規則：找不到要移除的")
                lines = kept
            else:
                errors.append(f"規則：不支援 {e.op}")
            if len(lines) > MAX_RULES:
                errors.append(f"規則：超過 {MAX_RULES} 條")
            out[target] = "\n".join(lines)
            continue
        if e.op not in ("replace", "insert_after", "delete"):
            errors.append(f"{target}: 不支援 {e.op}")
            continue
        if not e.anchor or body.count(e.anchor) != 1:
            errors.append(f"{target}: anchor 要一字不差地出現一次")
            continue
        new = {"replace": e.text, "insert_after": e.anchor + "\n" + e.text, "delete": ""}[e.op]
        out[target] = body.replace(e.anchor, new, 1)
    for target in ALWAYS_ON:
        if len(out[target]) > len(texts[target]):
            errors.append(f"{target}: 每輪都會送出，不能變長（多了 {len(out[target]) - len(texts[target])} 字）")
    grew = len(out["skill"]) - len(texts["skill"])
    if grew > SKILL_GROWTH or len(out["skill"]) > max(len(texts["skill"]), SKILL_MAX):
        errors.append(f"skill: 最多淨增 {SKILL_GROWTH} 字（多了 {grew}）")
    return out, errors


def bundle_for(skill: str, texts: dict[str, str], base: dict[str, str]):
    """The policy bundle a set of edited texts makes (runtime/policy.py)."""
    from meeseek.runtime import policy

    return policy.overlay(
        skills={skill: texts["skill"]} if texts["skill"] != base["skill"] else {},
        rules={skill: [ln for ln in texts["rules"].splitlines() if ln.strip()]} if texts["rules"] != base["rules"] else {},
        prompts={k.removeprefix("prompt:"): v for k, v in texts.items()
                 if k.startswith("prompt:") and v != base[k]})


# --- model calls ----------------------------------------------------------------------------

def _ask(prompt: str, schema, *, model: str, budget: Budget, step: str, usd: float):
    from meeseek.runtime.oneshot import ask

    if not budget.take(step, usd):
        return None
    try:
        return ask(prompt, model=model, schema=schema, temperature=0.2, max_output_tokens=6000, timeout_s=240,
                   source="rl_improve")
    except Exception:  # noqa: BLE001 — a step that did not happen, not a wrong one
        logger.warning("improve: %s failed", step, exc_info=True)
        return None


def evidence(skill: str, cluster: dict, texts: dict[str, str]) -> str:
    """The pack the proposers read: the cases, the person's words, the policy, past lessons."""
    from meeseek.runtime import cases

    parts = []
    by_id = {c["id"]: c for c in cases.load(skill)}
    for i, t in enumerate(cluster["turns"][:5], 1):
        case = next((by_id[cid] for cid in cluster["cases"] if t["inv"] in {s.get("inv") for s in by_id[cid]["src"]}),
                    None) if cluster["cases"] else None
        parts.append(f"### 第 {i} 輪\n請求：{t.get('request', '')[:800]}\n回答開頭：{t.get('reply_head', '')}\n"
                     f"呼叫：{', '.join(n + ('（失敗）' if f else '') for n, f in t.get('calls', []))}\n"
                     + (f"使用者要的：{case['reference']}\n" if case and case.get("reference") else ""))
    rejected = [p for p in proposals(status="rejected") if p.get("skill") == skill and p.get("note")][-3:]
    if rejected:
        parts.append("### 之前被你拒絕的修改\n" + "\n".join(f"- {p['summary']}：{p['note']}" for p in rejected))
    lessons_ = [ls for ls in lessons() if ls.get("skill") == skill][-5:]
    if lessons_:
        parts.append("### 之前的經驗\n" + "\n".join(f"- 情況：{ls['cue']} → {ls['lesson']}" for ls in lessons_))
    parts.append(f"### 這個 skill 的 SOP（{len(texts['skill'])} 字，最多淨增 {SKILL_GROWTH}）\n{texts['skill']}")
    parts.append(f"### 學到的規則（每輪都送出）\n{texts['rules'] or '（沒有）'}")
    return "\n\n".join(parts)


def propose(skill: str, cluster: dict, texts: dict[str, str], kind: str, budget: Budget) -> Candidate | None:
    from meeseek.config import cfg
    from meeseek.context import prompts

    rules = prompts.text("internal/proposer#rules", evidence=evidence(skill, cluster, texts))
    if kind == "inv":
        prompt = prompts.text("internal/proposer#inv", skill=skill, rules=rules)
    else:
        prompt = prompts.text("internal/proposer#dom", skill=skill, rules=rules)
    return _ask(prompt, Candidate, model=cfg.models.workflow, budget=budget, step=f"propose:{kind}", usd=PROPOSE_USD)


class Pair(BaseModel):
    winner: str = Field(description="A | B | tie")


def pair(case: dict, reply_a, reply_b, budget: Budget) -> str | None:
    """'A', 'B' or 'tie' for two replays of one case; None when not judged."""
    from meeseek.config import cfg
    from meeseek.context import prompts

    def calls(run):
        return ", ".join(c["name"] + ("（失敗）" if c["failed"] else "") for c in run.calls) or "（沒有）"

    prompt = prompts.text("internal/pair_judge#judge", request=case["target_turn"][:3000],
                          reference=case.get("reference") or "（沒有）", calls_a=calls(reply_a),
                          reply_a=reply_a.reply[:5000], calls_b=calls(reply_b), reply_b=reply_b.reply[:5000])
    got = _ask(prompt, Pair, model=cfg.models.fast, budget=budget, step="pair", usd=JUDGE_USD)
    return got.winner.strip().upper().replace("TIE", "tie") if got else None


def blind(case: dict, base, cand, budget: Budget) -> str:
    """'cand', 'base' or 'tie': judged both ways round, a disagreement is a tie."""
    first = pair(case, base, cand, budget)        # base is A
    second = pair(case, cand, base, budget)       # cand is A
    if first is None or second is None:
        return "tie"
    a = {"A": "base", "B": "cand"}.get(first, "tie")
    b = {"A": "cand", "B": "base"}.get(second, "tie")
    return a if a == b else "tie"


class Lesson(BaseModel):
    no_entry: bool = False
    cue: str = ""
    lesson: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: str = "low"
    verdicts: dict[str, str] = Field(default_factory=dict, description="每一處修改：helped | hurt | no_effect | confounded")


def reflect(skill: str, edits: list[Edit], outcomes: dict[str, str], budget: Budget) -> dict | None:
    """At most one lesson from the probe — from what happened, never from why a judge thought so."""
    from meeseek.config import cfg
    from meeseek.context import prompts

    shown = "\n".join(f"修改 {i}：{e.target} {e.op}「{e.anchor[:80]}」→「{e.text[:300]}」（{e.why}）"
                      for i, e in enumerate(edits)) + "\n\n" + \
        "\n".join(f"案例 {cid}：{'修改後比較好' if o == 'cand' else '原本比較好' if o == 'base' else '差不多'}"
                  for cid, o in outcomes.items())
    got = _ask(prompts.text("internal/reflector#reflect", skill=skill, evidence=shown), Lesson,
               model=cfg.models.workflow, budget=budget, step="reflect", usd=REFLECT_USD)
    if not got or got.no_entry or not got.lesson.strip():
        return None
    if _GRADER.search(got.lesson + got.cue):
        return None                               # a lesson is a way of working, never a score
    return {"cue": got.cue[:300], "lesson": got.lesson[:500], "evidence": got.evidence[:6],
            "confidence": got.confidence, "verdicts": got.verdicts}


# --- the run ------------------------------------------------------------------------------

def _replay(case: dict, bundle, budget: Budget):
    """One replay of `case` under `bundle`, booked; None when the budget is out."""
    import asyncio

    from meeseek.runtime.replays import run_session

    hint = case.get("cost_hint_usd", 0.065) * 1.3
    if not budget.can(hint, replays=1):
        return None
    run = asyncio.run(run_session([case["target_turn"]], prefix_events=case.get("prefix_events", []), bundle=bundle))
    budget.book("replay", run.cost_usd or hint, replays=1)
    return run


def probe(skill: str, slice_: list[dict], bundles: dict, budget: Budget) -> dict:
    """Every case under every policy, in one sitting: {case id: {name: TurnRun | None}}."""
    out: dict = {}
    for case in slice_:
        out[case["id"]] = {}
        for name, b in bundles.items():
            run = _replay(case, b, budget)
            out[case["id"]][name] = None if run is None or run.error or not run.turns else run.turns[0]
    return out


def score(slice_: list[dict], runs: dict, cand: str, budget: Budget) -> dict:
    """How a candidate did against the base: per case 'cand'/'base'/'tie'/'error'."""
    outcomes = {}
    for case in slice_:
        base, c = runs[case["id"]].get("base"), runs[case["id"]].get(cand)
        if base is None or c is None:
            outcomes[case["id"]] = "error"
            continue
        outcomes[case["id"]] = blind(case, base, c, budget)
    return outcomes


def run(skill: str | None = None, *, budget: Budget | None = None, dry_run: bool = False) -> dict:
    """One week's run; what it did. `dry_run` proposes nothing, only reports."""
    from meeseek.runtime import cases, policy, reward

    budget = budget or Budget()
    report: dict = {"week": _week(), "skill": skill, "steps": []}
    judged = reward.judge_unlabeled(limit=30, calibrate=5, spend=lambda usd: budget.take("judge", usd))
    report["steps"].append(f"judged {judged} turns")
    clusters = mine(skill)
    if not clusters:
        report["steps"].append("nothing to work on: no group of turns went badly enough")
        return report
    cluster = clusters[0]
    sk = cluster["skill"]
    if sk == "router" or not _skill_file(sk).exists():
        report["steps"].append(f"the worst group is {sk}'s; only skills are edited for now")
        return report
    texts = current(sk)
    candidates: dict[str, tuple[list[Edit], dict]] = {}
    for kind in ("inv", "dom")[:max(1, cfg.learning.k)]:
        cand = propose(sk, cluster, texts, kind, budget)
        if cand is None or cand.keep_base or not cand.edits:
            report["steps"].append(f"{kind}: keep as is")
            continue
        new, errors = apply_edits(sk, cand.edits + cand.cut, texts)
        if errors:
            report["steps"].append(f"{kind}: invalid — {'; '.join(errors)[:300]}")
            continue
        candidates[kind] = (cand.edits + cand.cut, new)
    if not candidates:
        return report
    negatives = cases.slice(sk, kind="negative", n=3, budget_usd=budget.usd, ids=cluster["cases"] or None)
    positives = cases.slice(sk, kind="positive", n=3, budget_usd=budget.usd)
    if not negatives:
        report["steps"].append("no replayable case for this group yet")
        return report
    bundles = {"base": None, **{k: bundle_for(sk, new, texts) for k, (_, new) in candidates.items()}}
    run_id = uuid.uuid4().hex[:8]
    runs = probe(sk, negatives + positives, bundles, budget)
    best, best_net, best_out = None, 0, {}
    for k in candidates:
        out = score(negatives + positives, runs, k, budget)
        neg = [out[c["id"]] for c in negatives]
        net = neg.count("cand") - neg.count("base")
        regress = any(out[c["id"]] == "base" for c in positives) or "error" in out.values()
        report["steps"].append(f"{k}: target {neg.count('cand')}-{neg.count('base')}, regression {'yes' if regress else 'no'}")
        if not regress and net >= MARGIN and net > best_net:
            best, best_net, best_out = k, net, out
    cases.record_run({"run_id": run_id, "kind": "probe", "skill": sk, "bundle": policy.bundle_id(),
                      "candidates": list(candidates), "cases": {cid: o for cid, o in best_out.items()},
                      "cost_usd": round(budget.spent()[0], 4)})
    for c in negatives + positives:   # how the base did, for the library's own bookkeeping
        base = runs[c["id"]].get("base")
        cases.record_result(c, run=run_id, bundle=policy.bundle_id(), outcome="error" if base is None else
                            ("pass" if best_out.get(c["id"]) in ("base", "tie") else "fail"))
    if best is None:
        report["steps"].append("keep as is: no candidate did better by the margin")
        return report
    confirm = probe(sk, negatives, {"base": None, best: bundles[best]}, budget)
    again = score(negatives, confirm, best, budget)
    if list(again.values()).count("cand") <= list(again.values()).count("base"):
        report["steps"].append(f"keep as is: {best} did not hold up on a second replay")
        return report
    edits, new = candidates[best]
    lesson = reflect(sk, edits, best_out, budget)
    if lesson:
        remember_lesson(sk, lesson)
    if dry_run:
        report["steps"].append(f"dry run: would propose {best}")
        report["proposal"] = {"edits": [e.model_dump() for e in edits]}
        return report
    p = file_proposal(sk, cluster, edits, texts, new, best_out, run_id, lesson, budget.spent()[0])
    report["steps"].append(f"proposed {p['id']}")
    report["proposal"] = p["id"]
    return report


# --- proposals ------------------------------------------------------------------------------

def _diff(before: str, after: str, name: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile=f"{name} (now)", tofile=f"{name} (proposed)", n=2))


def file_proposal(skill: str, cluster: dict, edits: list[Edit], before: dict, after: dict,
                  outcomes: dict, run_id: str, lesson: dict | None, cost: float) -> dict:
    changed = [k for k in before if before[k] != after[k]]
    p = {"id": uuid.uuid4().hex[:8], "kind": "edit", "skill": skill, "created": time.time(), "run_id": run_id,
         "summary": "; ".join(e.why or f"{e.target} {e.op}" for e in edits)[:200],
         "cluster": {"size": len(cluster["turns"]), "tags": cluster["tags"], "cases": cluster["cases"],
                     "requests": [t.get("request", "")[:120] for t in cluster["turns"][:3]]},
         "edits": [e.model_dump() for e in edits], "changed": changed,
         "after": {k: after[k] for k in changed}, "base_hashes": {k: _sha8(before[k]) for k in changed},
         "diff": "\n".join(_diff(before[k], after[k], k) for k in changed),
         "outcomes": outcomes, "lesson": lesson, "cost_usd": round(cost, 4)}
    proposals_store().append(p)
    return p


def _decisions() -> dict[str, dict]:
    """The last decision on each proposal (the "told you about it" marks are not decisions)."""
    last: dict[str, dict] = {}
    for d in decisions_store().read():
        if d.get("decision"):
            last[d["id"]] = d
    return last


def proposals(status: str = "") -> list[dict]:
    """Every proposal with its status: pending, approved, rejected, stale, expired, failed."""
    decided = _decisions()
    out = []
    for p in proposals_store().read():
        d = decided.get(p["id"], {})
        st = d.get("decision") or ("expired" if time.time() - p["created"] > EXPIRE_DAYS * 86400 else "pending")
        row = {**p, "status": st, "note": d.get("note", ""), "commit": d.get("commit", ""), "decided": d.get("at")}
        if not status or st == status:
            out.append(row)
    return out


def decide(pid: str, approve: bool, note: str = "", by: str = "you") -> dict:
    """Your decision on a proposal. Approving applies it (server process): the
    texts it was made against must still be as they were, or it is stale."""
    p = next((x for x in proposals() if x["id"] == pid), None)
    if p is None or p["status"] != "pending":
        raise ValueError("this proposal is not waiting for a decision")
    if not approve:
        decisions_store().append({"id": pid, "decision": "rejected", "note": note[:1000], "by": by, "at": time.time()})
        return {"status": "rejected"}
    try:
        result = promote(p) if p["kind"] == "edit" else rollback(p)
    except _Stale as e:
        decisions_store().append({"id": pid, "decision": "stale", "note": str(e), "by": by, "at": time.time()})
        return {"status": "stale", "why": str(e)}
    except Exception as e:  # noqa: BLE001
        logger.exception("improve: applying %s failed", pid)
        decisions_store().append({"id": pid, "decision": "failed", "note": str(e)[:300], "by": by, "at": time.time()})
        return {"status": "failed", "why": str(e)[:300]}
    decisions_store().append({"id": pid, "decision": "approved", "note": note[:1000], "by": by, "at": time.time(), **result})
    return {"status": "approved", **result}


class _Stale(Exception):
    pass


def promote(p: dict) -> dict:
    """Write an approved edit, commit what is tracked, and take it live."""
    from meeseek.context import prompts
    from meeseek.runtime import policy
    from meeseek.runtime.learned_rules import CONSOLE, forget, parse_learned, parse_line, write_learned_rule

    skill = p["skill"]
    now = current(skill)
    for k, h in p["base_hashes"].items():
        if _sha8(now[k]) != h:
            raise _Stale(f"{k} changed since the proposal was made")
    from_bundle = policy.bundle_id()
    tracked = []
    for k, text in p["after"].items():
        if k == "skill":
            path = _skill_file(skill)
            raw = path.read_text(encoding="utf-8")
            body = now["skill"]
            if raw.count(body.strip()) != 1:
                raise _Stale("SKILL.md body could not be located exactly")
            path.write_text(raw.replace(body.strip(), text.strip(), 1), encoding="utf-8")   # frontmatter untouched
            tracked.append(path)
        elif k == "rules":
            before = {r.text for r in parse_learned(skill)}
            after = [parse_line(ln) for ln in text.splitlines() if parse_line(ln)]
            for r in after:
                if r.text not in before:
                    msg = write_learned_rule(skill, r.text, "每週改進，經你同意", CONSOLE)
                    if not msg.startswith("✅"):
                        raise RuntimeError(msg)
            for r in parse_learned(skill):
                if r.text not in {a.text for a in after}:
                    forget(skill, r.id)
        elif k.startswith("prompt:"):
            name = k.removeprefix("prompt:")
            path = prompts.PROMPTS_DIR / f"{name}.md"
            path.write_text(text, encoding="utf-8")
            tracked.append(path)
    commit = ""
    if tracked:
        rels = [str(t.resolve()) for t in tracked]
        msg = (f"{skill}: adopt a weekly improvement (proposal {p['id']})\n\n{p['summary']}\n\n"
               f"Probe: {list(p['outcomes'].values()).count('cand')} cases better, cost US${p['cost_usd']}")
        subprocess.run(["git", "-C", str(home().skills), "commit", "-q", "-m", msg, "--", *rels],
                       check=True, capture_output=True, text=True)
        commit = subprocess.run(["git", "-C", str(home().skills), "rev-parse", "--short", "HEAD"],
                                check=True, capture_output=True, text=True).stdout.strip()
    policy.reload()
    return {"commit": commit, "from_bundle": from_bundle, "to_bundle": policy.bundle_id(), "applied_at": time.time()}


def rollback(p: dict) -> dict:
    """Undo an approved proposal: revert its commit, take the rules it added out."""
    from meeseek.runtime import policy
    from meeseek.runtime.learned_rules import forget, parse_learned

    target = next((x for x in proposals() if x["id"] == p["of"]), None)
    commit = ""
    if target and target.get("commit"):
        subprocess.run(["git", "-C", str(home().skills), "revert", "--no-edit", target["commit"]],
                       check=True, capture_output=True, text=True)
        commit = subprocess.run(["git", "-C", str(home().skills), "rev-parse", "--short", "HEAD"],
                                check=True, capture_output=True, text=True).stdout.strip()
    if target and "rules" in target.get("after", {}):
        added = {ln for ln in target["after"]["rules"].splitlines()}
        for r in parse_learned(target["skill"]):
            if r.raw in added and "每週改進" in r.raw:
                forget(target["skill"], r.id)
    policy.reload()
    return {"commit": commit}


def monitor(now: float | None = None) -> int:
    """Nightly: an approved change that made its skill do worse gets a rollback
    proposal — for you to approve, like any other. How many were filed."""
    from meeseek.runtime import reward

    now = now or time.time()
    filed = 0
    open_rollbacks = {p.get("of") for p in proposals() if p["kind"] == "rollback" and p["status"] == "pending"}
    for p in proposals(status="approved"):
        if p["kind"] != "edit" or p["id"] in open_rollbacks or not p.get("decided"):
            continue
        at = p["decided"]
        pairs = [(t, r) for t, r in reward.rewards(60) if t.get("skill") == p["skill"] and r.value is not None]
        after = [r.value for t, r in pairs if t["at"] >= at]
        before = [r.value for t, r in pairs if at - 28 * 86400 <= t["at"] < at]
        if len(after) < 15 and now - at < 14 * 86400:
            continue
        if len(after) < 5 or len(before) < 5:
            continue
        drop = (sum(before) / len(before)) - (sum(after) / len(after))
        if drop > 0.15:
            proposals_store().append({"id": uuid.uuid4().hex[:8], "kind": "rollback", "of": p["id"], "skill": p["skill"],
                              "created": now, "summary": f"undo {p['id']}: {p['skill']} did worse since (reward -{drop:.2f})",
                              "diff": p.get("diff", ""), "outcomes": {}, "cost_usd": 0, "lesson": None})
            filed += 1
    return filed


# --- lessons --------------------------------------------------------------------------------

def remember_lesson(skill: str, lesson: dict) -> None:
    """A lesson, merged into one it repeats (context/lifecycle.py)."""
    now = time.time()
    same = next((ls for ls in lessons() if ls.get("skill") == skill and lifecycle.same(ls["lesson"], lesson["lesson"])),
                None)
    if same:
        lessons_store().append({**same, "seen": same.get("seen", 1) + 1, "last_seen": now})
    else:
        lessons_store().append({"id": uuid.uuid4().hex[:8], "skill": skill, **lesson, "seen": 1, "at": now, "last_seen": now})


def lessons(now: float | None = None) -> list[dict]:
    """Live lessons: the latest record of each, those past their time left out."""
    last: dict[str, dict] = {}
    for ls in lessons_store().read():
        last[ls["id"]] = ls
    return [ls for ls in last.values()
            if not lifecycle.expired(confirmed=False, seen=ls.get("seen", 1), last_seen=ls.get("last_seen", 0), now=now)]


# --- notices --------------------------------------------------------------------------------

def unannounced() -> list[dict]:
    """Pending proposals nobody has been told about yet."""
    told = {d["id"] for d in decisions_store().read() if d.get("announced")}
    return [p for p in proposals(status="pending") if p["id"] not in told]


def announced(pid: str) -> None:
    decisions_store().append({"id": pid, "announced": True, "at": time.time()})


def tidy(now: float | None = None) -> int:
    """Nightly: lessons past their time, budget weeks past eight, and proposals
    decided or expired more than ninety days ago leave the files; how many lines."""
    now = now or time.time()
    gone = 0
    live = {ls["id"] for ls in lessons(now)}
    before = len(lessons_store().read())
    lessons_store().update(lambda rs: [r for r in rs if r["id"] in live])
    gone += before - len(lessons_store().read())
    weeks = {time.strftime("%G-W%V", time.localtime(now - d * 7 * 86400)) for d in range(8)}
    before = len(budget_store().read())
    budget_store().update(lambda rs: [r for r in rs if r.get("week") in weeks])
    gone += before - len(budget_store().read())
    old = {p["id"] for p in proposals() if p["status"] != "pending" and now - p["created"] > 90 * 86400}
    if old:
        proposals_store().update(lambda rs: [r for r in rs if r["id"] not in old])
        decisions_store().update(lambda rs: [r for r in rs if r["id"] not in old])
        gone += len(old)
    return gone


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="meeseek's weekly improvement run (runtime/improve.py)")
    ap.add_argument("--skill", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    print(json.dumps(run(args.skill, dry_run=args.dry_run), ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
