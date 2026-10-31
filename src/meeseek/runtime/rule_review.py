"""Weekly rule review: keeps every rule in learned.md earning its place.

For every learned rule with a kept case, the request that led to it is
replayed in a fresh session with and without the rule, and a fast model
judges whether each reply follows it:

  working       followed with it, not without — the rule makes the difference
  redundant     followed either way — the SOP already covers it
  not_followed  not followed even with it — the rule does not work

One replay is noisy, so a verdict acts only when the previous review agreed:
two `redundant` or two `not_followed` remove the rule (archived, restorable);
two `working` mark it ready to graduate into SKILL.md, which a person
confirms because SKILL.md is committed. A rule with no case cannot be
replayed; after STALE_DAYS it is listed for a person to decide.

Each review appends one record to history_file(); the Learning page shows the
latest as the weekly summary.

Run as its own process (`python -m meeseek.runtime.rule_review`): the
server spawns it weekly and from the page's button, so a replay never shares
an event loop or MCP connections with live turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from datetime import date, datetime, timezone

from pydantic import BaseModel, Field

from meeseek.config import PROJECT_ROOT, cfg
from meeseek.config.paths import home
from meeseek.runtime import learned_rules as lr
from meeseek.runtime.store import JsonlStore

logger = logging.getLogger(__name__)


def history_file() -> Path:
    return home().data / "rule_reviews.jsonl"


def archive_file() -> Path:
    return home().data / "learned_archive.jsonl"


def lock_file() -> Path:
    return home().data / "rule_review.running"


STALE_DAYS = 30
# A replay waiting on a tool server that died never returns on its own.
_REPLAY_TIMEOUT_S = 600
_JUDGE_TIMEOUT_S = 120
_STALE_LOCK_S = 3 * 3600  # a run that left its lock behind (killed) stops blocking after this

REMOVE = {"redundant": "Two reviews: followed without it too (the SOP covers it)",
          "not_followed": "Two reviews: not followed even with it"}


class Verdict(BaseModel):
    follows: bool = Field(description="回覆是否遵守這條規則")
    why: str = Field(description="一句理由，引用回覆裡的證據")


def judge(rule: str, prompt: str, reply: str, calls: list[dict]) -> dict:
    """Whether a reply follows a rule, by a fast model; follows=None if it could not tell."""
    from meeseek.context import prompts
    from meeseek.runtime.oneshot import ask

    names = "\n".join(f"- {c['name']}({json.dumps(c.get('args', {}), ensure_ascii=False)[:1500]})"
                      for c in calls) or "（無）"
    try:
        v = ask(prompts.text("internal/rule_judge", rule=rule, prompt=prompt, calls=names, reply=reply[:6000]),
                model=cfg.models.fast, schema=Verdict, temperature=0)
        return {"follows": v.follows, "why": v.why}
    except Exception as e:
        return {"follows": None, "why": f"judge failed: {type(e).__name__}: {e}"}


def _first_request(case: dict) -> str:
    for turn in case.get("turns", []):
        parts = (turn.get("user_content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if text:
            return text
    return ""


def verdict_of(with_rule: dict, without_rule: dict) -> str:
    """What the two judgements say about the rule."""
    w, wo = with_rule.get("follows"), without_rule.get("follows")
    if w is None or wo is None:
        return "unknown"
    if w and not wo:
        return "working"
    if w and wo:
        return "redundant"
    return "not_followed"


def library_case(skill: str, rule: lr.LearnedRule) -> dict | None:
    """The rule's own conversation in the case library (runtime/cases.py), or —
    for a rule written from the console, which has none — a case whose request
    matches the situation the rule is for (its cue)."""
    from meeseek.runtime import cases

    live = [c for c in cases.load(skill) if c.get("status") != "retired" and c.get("target_turn")]
    own = next((c for c in live if c.get("origin") == f"rule:{rule.id}"), None)
    if own or not rule.cue:
        return own
    return next((c for c in live if lr.similarity(rule.cue, c.get("request", "")) >= lr.SIMILAR), None)


async def _check_case(skill: str, rule: lr.LearnedRule, case: dict) -> dict:
    """The case replayed in its conversation, with the rule and with a policy
    that leaves it out (runtime/policy.py), each reply judged."""
    from meeseek.runtime import policy
    from meeseek.runtime.replays import run_session

    without = policy.overlay(rules={skill: [r.raw for r in lr.parse_learned(skill) if r.id != rule.id]})
    results = {}
    for label, bundle in (("with", None), ("without", without)):
        try:
            run = await asyncio.wait_for(run_session([case["target_turn"]], prefix_events=case.get("prefix_events", []),
                                                     bundle=bundle), _REPLAY_TIMEOUT_S)
        except asyncio.TimeoutError:
            results[label] = {"follows": None, "why": f"replay timed out after {_REPLAY_TIMEOUT_S}s",
                              "reply": "", "error": "timeout"}
            continue
        turn = run.turns[0] if run.turns else None
        reply, calls = (turn.reply, turn.calls) if turn else ("", [])
        try:
            verdict = await asyncio.wait_for(
                asyncio.to_thread(judge, rule.text, case["target_turn"], reply, calls), _JUDGE_TIMEOUT_S)
        except asyncio.TimeoutError:
            verdict = {"follows": None, "why": f"judge timed out after {_JUDGE_TIMEOUT_S}s"}
        results[label] = {**verdict, "reply": reply[:500], "error": run.error}
    return {"prompt": case["target_turn"][:500], "case": case["id"], **results,
            "verdict": verdict_of(results["with"], results["without"])}


async def check(skill: str, rule: lr.LearnedRule) -> dict:
    """One rule replayed with and without it, judged — in the conversation it
    came from when the case library has it, else its first request alone."""
    from meeseek.runtime.replays import replay

    rec = {"skill": skill, "id": rule.id, "text": rule.text, "date": rule.date}
    case = library_case(skill, rule)
    if case is not None:
        return {**rec, **(await _check_case(skill, rule, case))}
    prompt = _first_request(lr.load_case(skill, rule.id) or {})
    if not prompt:
        return {**rec, "verdict": "no_case"}
    results = {}
    for label, exclude in (("with", ""), ("without", rule.id)):
        try:
            r = await asyncio.wait_for(replay(prompt, exclude_rule=exclude), _REPLAY_TIMEOUT_S)
        except asyncio.TimeoutError:
            results[label] = {"follows": None, "why": f"replay timed out after {_REPLAY_TIMEOUT_S}s",
                              "reply": "", "error": "timeout"}
            continue
        try:
            verdict = await asyncio.wait_for(
                asyncio.to_thread(judge, rule.text, prompt, r.reply, r.calls), _JUDGE_TIMEOUT_S)
        except asyncio.TimeoutError:
            verdict = {"follows": None, "why": f"judge timed out after {_JUDGE_TIMEOUT_S}s"}
        results[label] = {**verdict, "reply": r.reply[:500], "error": r.error}
    return {**rec, "prompt": prompt, **results,
            "verdict": verdict_of(results["with"], results["without"])}


def action_for(verdict: str, previous: str | None, rule_date: str, today: date) -> str:
    """What a review does with a rule: remove, graduate (a person confirms),
    stale (a person decides), or nothing."""
    if verdict in REMOVE and previous == verdict:
        return "remove"
    if verdict == "working" and previous == "working":
        return "graduate"
    if verdict == "no_case":
        try:
            age = (today - date.fromisoformat(rule_date)).days
        except ValueError:
            age = STALE_DAYS
        if age >= STALE_DAYS:
            return "stale"
    return ""


def history() -> list[dict]:
    """Every review, oldest first."""
    return JsonlStore(history_file()).read()


def _previous_verdicts() -> dict[tuple[str, str], str]:
    """(skill, rule id) → the verdict the last review that checked it gave."""
    out: dict[tuple[str, str], str] = {}
    for rec in history():
        for r in rec.get("rules", []):
            out[(r["skill"], r["id"])] = r["verdict"]
    return out


def archive(skill: str, rule: lr.LearnedRule, why: str) -> None:
    """Take a rule out of learned.md, keeping it where it can be restored from."""
    JsonlStore(archive_file()).append({
        "skill": skill, "id": rule.id, "text": rule.text, "raw": rule.raw, "why": why,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    lr.forget(skill, rule.id)


def archived() -> list[dict]:
    """Archived rules, newest first."""
    return list(reversed(JsonlStore(archive_file()).read()))


def restore(skill: str, rid: str) -> None:
    """Put an archived rule back into learned.md, as it was."""
    store = JsonlStore(archive_file())
    rec = next((r for r in store.read() if r["skill"] == skill and r["id"] == rid), None)
    if rec is None:
        raise ValueError(f"not in the archive: {skill}/{rid}")
    if lr.count_rules(skill) >= lr.MAX_RULES:
        raise ValueError(f"'{skill}' already has {lr.MAX_RULES} rules")
    lr.append_raw(skill, rec["raw"])
    store.update(lambda rows: [r for r in rows if not (r["skill"] == skill and r["id"] == rid)])


RULES_PER_WEEK = 6           # the weekly budget is shared with the improvement run (runtime/improve.py)
_RULE_USD = 0.17             # two replays of a median turn, and the judge


async def run(today: date | None = None, budget=None) -> dict:
    """Review the learned rules — up to RULES_PER_WEEK, what the budget allows —
    act on what two reviews agree on, record it."""
    today = today or date.today()
    previous = _previous_verdicts()
    reviewed = 0
    record: dict = {"started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "rules": [], "actions": []}
    for skill_dir in sorted(home().skills.iterdir()):
        for rule in lr.parse_learned(skill_dir.name):
            if budget is not None:
                if reviewed >= RULES_PER_WEEK or not budget.can(_RULE_USD, replays=2):
                    continue
                budget.book("rule_review", _RULE_USD, replays=2)
            reviewed += 1
            checked = await check(skill_dir.name, rule)
            record["rules"].append(checked)
            action = action_for(checked["verdict"], previous.get((skill_dir.name, rule.id)),
                                 rule.date, today)
            if not action:
                continue
            why = REMOVE.get(checked["verdict"], "")
            if action == "remove":
                archive(skill_dir.name, rule, why)
            record["actions"].append({"skill": skill_dir.name, "id": rule.id, "text": rule.text,
                                      "action": action, "why": why})
    record["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    JsonlStore(history_file()).append(record)
    return record


def _running() -> bool:
    if not lock_file().exists():
        return False
    try:
        started = datetime.fromisoformat(lock_file().read_text(encoding="utf-8").strip())
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - started).total_seconds() < _STALE_LOCK_S


def load() -> dict:
    """The latest review, and whether one is in progress."""
    reviews = history()
    return {"latest": reviews[-1] if reviews else None, "running": _running()}


def spawn() -> bool:
    """Start a review in its own process; False if one is already running."""
    if _running():
        return False
    subprocess.Popen([sys.executable, "-m", "meeseek.runtime.rule_review"], cwd=PROJECT_ROOT,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    lock_file().parent.mkdir(parents=True, exist_ok=True)
    lock_file().write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    try:
        from meeseek.runtime import improve

        budget = improve.Budget()
        asyncio.run(run(budget=budget))
        # Then the week's improvement run, under the same lock and budget.
        try:
            report = improve.run(budget=budget)
            logging.getLogger(__name__).info("improve: %s", report.get("steps"))
        except Exception:  # noqa: BLE001 — the rule review is recorded all the same
            logging.getLogger(__name__).exception("improve: the weekly run failed")
    finally:
        lock_file().unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
