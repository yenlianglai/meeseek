"""Long-term memory: what every agent is told before each turn.

`memory_instruction` assembles it — pinned notes (hand-written, notes/pinned/),
what is known about you (context/personal.py), what meetings showed of the team
beyond the roster (context/ontology.py `patterns`), learned facts
(notes/facts.md, the newest 20 within 90 days) and, for a skill agent, that
skill's learned rules. Every agent receives it as its per-turn dynamic
instruction, so an edit or a newly remembered fact takes effect on the next
turn. Each learned section has its budget (personal.SUMMARY_CHARS,
ontology.PATTERNS_CHARS, MAX_FACTS, learned_rules.MAX_RULES); the pinned notes
are a person's to keep short.

Facts are written only by `append_fact`, after the user agrees in chat. Each
line carries its date and source; new lines are written as `explicit`, and
lines written earlier under other tags still load. A fact past its window is
taken out of the file (`tidy_facts`, at each write and nightly): it had
already stopped reaching the agents; one meant to stay is moved to
notes/pinned/.
"""

from __future__ import annotations

import re
from pathlib import Path
from datetime import date, timedelta

from meeseek.config.paths import home
from meeseek.runtime.store import path_lock


def pinned_dir() -> Path:
    return home().notes / "pinned"


# The only source a new fact is written under: the user asked for it to be remembered.
def facts_file() -> Path:
    return home().notes / "facts.md"


EXPLICIT = "explicit"

# How long a fact stays in the per-turn memory after the date stamped on it.
FACTS_WINDOW_DAYS = 90
# How many of the newest facts reach an agent; older ones wait out their window unread.
MAX_FACTS = 20

# A fact bullet ends in "(2026-09-17)" or "(2026-09-17, <source>)".
_FACT_DATE = re.compile(r"\((\d{4}-\d{2}-\d{2})(?:,\s*[\w-]+)?\)\s*$")


def fact_date(line: str) -> date | None:
    """The date stamped on a fact bullet, or None if the line is not a dated bullet."""
    m = _FACT_DATE.search(line)
    if not (line.lstrip().startswith("- ") and m):
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def load_facts(max_age_days: int | None = FACTS_WINDOW_DAYS, today: date | None = None) -> str:
    """Learned facts dated within `max_age_days`, at most MAX_FACTS of the
    newest. The file itself is not modified."""
    if not facts_file().exists():
        return ""
    text = facts_file().read_text(encoding="utf-8")
    if max_age_days is None:
        return text
    cutoff = (today or date.today()) - timedelta(days=max_age_days)
    lines = text.splitlines()
    dated = sorted(((d, i) for i, ln in enumerate(lines) if (d := fact_date(ln)) and d > cutoff),
                   reverse=True)
    newest = {i for _, i in dated[:MAX_FACTS]}
    return "\n".join(ln for i, ln in enumerate(lines) if fact_date(ln) is None or i in newest)


def tidy_facts(today: date | None = None) -> int:
    """Take the dated facts past FACTS_WINDOW_DAYS out of facts.md; how many."""
    if not facts_file().exists():
        return 0
    cutoff = (today or date.today()) - timedelta(days=FACTS_WINDOW_DAYS)
    with path_lock(facts_file()):
        lines = facts_file().read_text(encoding="utf-8").splitlines()
        kept = [ln for ln in lines if (d := fact_date(ln)) is None or d > cutoff]
        if len(kept) != len(lines):
            facts_file().write_text("\n".join(kept) + "\n", encoding="utf-8")
    return len(lines) - len(kept)


def append_fact(fact: str, source: str = EXPLICIT) -> str:
    """Append a consented fact to facts.md, stamped with date and source.

    Consent is the caller's job. `source` must be EXPLICIT; anything else raises."""
    if source != EXPLICIT:
        raise ValueError(f"'{source}' 不是記憶的來源；只接受 {EXPLICIT}（使用者要求記住）")
    from meeseek.runtime.learned_rules import nearest

    known = [ln[2:] for ln in load_facts(None).splitlines() if ln.startswith("- ")]
    known += [ln for ln in load_pinned_notes().splitlines() if ln.strip()]
    same = nearest(fact, known)
    if same:
        raise ValueError(f"已經記得這件事了：「{same.strip()[:120]}」")
    tidy_facts()
    entry = f"- {fact.strip()} ({date.today().isoformat()}, {source})"
    # Locked: concurrent turns may append at once, and this is a read-modify-write.
    with path_lock(facts_file()):
        facts_file().parent.mkdir(parents=True, exist_ok=True)
        existing = facts_file().read_text(encoding="utf-8") if facts_file().exists() else "# Learned Facts\n"
        facts_file().write_text(existing.rstrip() + "\n" + entry + "\n", encoding="utf-8")
    return f"✅ 已記住：{entry}（注入 {FACTS_WINDOW_DAYS} 天後自動淡出）"


def load_pinned_notes() -> str:
    """Every notes/pinned/*.md except README, concatenated."""
    if not pinned_dir().exists():
        return ""
    parts = []
    for f in sorted(pinned_dir().glob("*.md")):
        if f.name.lower() == "readme.md":
            continue
        content = f.read_text(encoding="utf-8").strip()
        if content:
            parts.append(content)
    return "\n\n---\n\n".join(parts)


def memory_instruction(skill: str | None = None) -> str:
    """The per-turn memory block: pinned notes, what is known about you
    (context/personal.py), learned facts, and — for a skill agent — that
    skill's consented rules."""
    from meeseek.context import prompts
    from meeseek.runtime.learned_rules import load_learned_rules

    sections = []
    pinned = load_pinned_notes()
    if pinned:
        sections.append(prompts.text("blocks/memory#pinned", notes=pinned))
    try:
        from meeseek.context import personal

        me = personal.summary()
    except Exception:  # noqa: BLE001 — a broken personal store never costs a turn
        me = ""
    if me:
        sections.append(prompts.text("blocks/memory#me", me=me))
    try:
        from meeseek.context import ontology

        team = ontology.patterns()
    except Exception:  # noqa: BLE001 — nor does a broken team graph
        team = ""
    if team:
        sections.append(prompts.text("blocks/memory#team", team=team))
    facts = load_facts()
    if facts.count("\n- ") > 0:
        sections.append(prompts.text("blocks/memory#facts", facts=facts))
    rules = load_learned_rules(skill) if skill else ""
    if rules:
        sections.append(prompts.text("blocks/memory#rules", rules=rules))
    return "\n\n".join(sections)


def remember_fact(fact: str) -> str:
    """記住一個專案／環境／團隊事實：每個 agent 每輪都看得到，90 天後淡出。

    先用 `ask_user` 問「要我記住這件事嗎？」，同意後才呼叫。

    Args:
        fact: 一句事實（人事物/約定/環境情況）
    """
    return append_fact(fact, source=EXPLICIT)
