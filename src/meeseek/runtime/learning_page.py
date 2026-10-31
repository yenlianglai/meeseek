"""What the console's Learning page shows and the actions it takes.

Shows the weekly review (runtime/rule_review.py), learned rules with their
case and verdicts, archived rules, and facts with whether each still reaches
agents. Rules can be graduated into SKILL.md, edited, deleted or restored;
facts can be edited, pinned (never expire) or deleted.
"""

from __future__ import annotations

from datetime import date, timedelta

from meeseek.config.paths import home
from meeseek.context import memory
from meeseek.runtime import learned_rules as lr
from meeseek.runtime import rule_review
from meeseek.runtime.store import path_lock

PINNED_FACTS = "facts.md"   # notes/pinned/facts.md — where pinned facts go
_PINNED_HEADER = "# 已確認的事實（由記憶併入，不會過期）\n"


def _case_summary(skill: str, rid: str) -> dict | None:
    case = lr.load_case(skill, rid)
    if not case:
        return None
    users = [((t.get("user_content") or {}).get("parts") or [{}])[0].get("text", "")
             for t in case.get("turns", [])]
    return {"created": case.get("created", ""), "session_id": case.get("session_id", ""),
            "user_id": case.get("user_id", ""), "turns": [u for u in users if u]}


def _verdicts() -> dict[tuple[str, str], list[dict]]:
    """(skill, rule id) → that rule's checks, oldest first."""
    out: dict[tuple[str, str], list[dict]] = {}
    for rec in rule_review.history():
        for r in rec.get("rules", []):
            out.setdefault((r["skill"], r["id"]), []).append({**r, "at": rec.get("started", "")})
    return out


def rules() -> list[dict]:
    checks = _verdicts()
    out = []
    for skill_dir in sorted(home().skills.iterdir()):
        for r in lr.parse_learned(skill_dir.name):
            history = checks.get((skill_dir.name, r.id), [])
            out.append({"skill": skill_dir.name, "id": r.id, "text": r.text, "date": r.date,
                        "source": r.source, "reason": r.reason,
                        "case": _case_summary(skill_dir.name, r.id),
                        "check": history[-1] if history else None,
                        "verdicts": [h["verdict"] for h in history]})
    return out


def review() -> dict:
    """The latest weekly review, whether one is running, and the archive."""
    return {**rule_review.load(), "archive": rule_review.archived()}


def restore_rule(skill: str, rid: str) -> None:
    rule_review.restore(skill, rid)


def skills() -> list[str]:
    """Skills a rule can be written for."""
    return sorted(d.name for d in home().skills.iterdir() if (d / "SKILL.md").exists())


def add_rule(skill: str, text: str, reason: str) -> str:
    return lr.write_learned_rule(skill, text, reason or "在 console 寫入", source=lr.CONSOLE)


def edit_rule(skill: str, rid: str, text: str, reason: str) -> str:
    return lr.update_rule(skill, rid, text, reason)


def graduate_rule(skill: str, rid: str) -> str:
    return lr.graduate(skill, rid)


def forget_rule(skill: str, rid: str) -> None:
    lr.forget(skill, rid)


def facts(today: date | None = None) -> list[dict]:
    """Every fact, and whether it still reaches agents: within its window and
    among the newest memory.MAX_FACTS."""
    today = today or date.today()
    if not memory.facts_file().exists():
        return []
    injected = set(memory.load_facts(today=today).splitlines())
    out = []
    for line in memory.facts_file().read_text(encoding="utf-8").splitlines():
        d = memory.fact_date(line)
        if d is None:
            continue
        expires = d + timedelta(days=memory.FACTS_WINDOW_DAYS)
        out.append({"text": _fact_text(line), "line": line, "date": d.isoformat(),
                    "expires": expires.isoformat(), "live": line in injected})
    return out


def _fact_text(line: str) -> str:
    return memory._FACT_DATE.sub("", line.lstrip()[2:]).strip()


def _drop_fact(line: str) -> None:
    with path_lock(memory.facts_file()):
        kept = [ln for ln in memory.facts_file().read_text(encoding="utf-8").splitlines(keepends=True)
                if ln.rstrip("\n") != line]
        if len(kept) == len(memory.facts_file().read_text(encoding="utf-8").splitlines()):
            raise ValueError("no such fact")
        memory.facts_file().write_text("".join(kept), encoding="utf-8")


def forget_fact(line: str) -> None:
    _drop_fact(line)


def pin_fact(line: str) -> None:
    """Move a fact into the pinned notes, where it reaches every agent with no expiry."""
    path = memory.pinned_dir() / PINNED_FACTS
    with path_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else _PINNED_HEADER
        path.write_text(f"{existing.rstrip()}\n- {_fact_text(line)}\n", encoding="utf-8")
    _drop_fact(line)
