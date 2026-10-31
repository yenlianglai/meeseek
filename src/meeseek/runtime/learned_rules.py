"""Learned rules — the write path for a rule the user agreed to.

Rules live in `skills/<name>/learned.md`, apart from the human-authored
SKILL.md, and are injected through the dynamic instruction, so a rule takes
effect next turn without a restart. Each passes a quality gate, is refused if
it repeats a rule or SOP line, is capped at MAX_RULES per skill, and carries
its date, source and reason on the line.

A rule written from a conversation keeps the turns that led to it as its case
(CASES_DIR/<skill>/<rule id>.json), so it can be replayed later. `graduate`
moves a rule into SKILL.md and commits that file.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from meeseek.config.paths import home
from meeseek.context import prompts
from meeseek.runtime.store import path_lock

logger = logging.getLogger(__name__)

MAX_RULES = 10

# Sources a new rule may be written under: agreed in chat, or written on the
# console. Other tags still parse; a bullet with none reads as LEGACY_SOURCE.
IN_SESSION = "in-session"
CONSOLE = "console"
SOURCES = (IN_SESSION, CONSOLE)
LEGACY_SOURCE = "legacy"

_MIN_LEN, _MAX_LEN = 15, 300

# Character-bigram containment at or above this is the same rule reworded
# (measured: rewrites of one rule 0.84–0.98, distinct rules 0.16–0.35).
SIMILAR = 0.6


def cases_dir() -> Path:
    return home().data / "learned_cases"


GRADUATED_HEADING = "## 團隊慣例（由學習規則併入）"
_CASE_TURNS = 3

LEARNED_HEADER = "# 學習規則（經使用者同意後寫入；每 turn 動態注入）\n"


def _learned_path(skill_name: str) -> Path:
    return home().skills / skill_name / "learned.md"


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"[\W_]+", "", _BULLET_META.sub("", text).lower())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similarity(a: str, b: str) -> float:
    """Share of the shorter text's character bigrams found in the other."""
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / max(1, min(len(x), len(y)))


def nearest(text: str, pool: list[str]) -> str | None:
    """The entry of `pool` that says the same thing as `text`, if any."""
    best = max(pool, key=lambda p: similarity(text, p), default=None)
    return best if best is not None and similarity(text, best) >= SIMILAR else None


def rule_id(text: str) -> str:
    import hashlib

    return hashlib.sha1(re.sub(r"\s+", "", text).encode()).hexdigest()[:8]


def _sop_lines(skill_name: str) -> list[str]:
    path = home().skills / skill_name / "SKILL.md"
    body = path.read_text(encoding="utf-8").split("---", 2)[-1] if path.exists() else ""
    return [ln.strip(" -*>") for ln in body.splitlines() if len(_bigrams(ln)) >= 15]


@dataclass(frozen=True)
class LearnedRule:
    """One bullet of a skill's learned.md, parsed."""
    text: str          # the rule itself, without the trailing metadata
    date: str          # YYYY-MM-DD, or "" if the bullet has no date
    source: str        # who proposed it; "legacy" if the bullet records none
    reason: str
    raw: str           # the original line, for rewrites
    cue: str = ""      # the situation it is for ("開 Jira 單時"): replayed on cases that match it

    @property
    def id(self) -> str:
        return rule_id(self.text)


_BULLET_META = re.compile(r"_（(?P<date>\d{4}-\d{2}-\d{2})(?:，(?P<source>[a-z-]+))?，依據：(?P<reason>.*?)"
                          r"(?:，情境：(?P<cue>.*?))?）_\s*$")


def parse_line(line: str) -> LearnedRule | None:
    """One learned.md bullet as a rule; None for any other line."""
    if not line.startswith("- "):
        return None
    body = line[2:].strip()
    m = _BULLET_META.search(body)
    meta = m.groupdict() if m else {}
    return LearnedRule(
        text=_BULLET_META.sub("", body).strip(),
        date=meta.get("date") or "",
        source=meta.get("source") or LEGACY_SOURCE,
        reason=(meta.get("reason") or "").strip(),
        raw=line,
        cue=(meta.get("cue") or "").strip(),
    )


def parse_learned(skill_name: str) -> list[LearnedRule]:
    """Every rule in a skill's learned.md — or, under a replay's policy bundle
    that names this skill's rules (runtime/policy.py), the bundle's."""
    from meeseek.runtime.policy import active

    b = active()
    if b is not None and skill_name in b.rules:
        lines = list(b.rules[skill_name])
    else:
        path = _learned_path(skill_name)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
    return [r for line in lines if (r := parse_line(line)) is not None]


# Rule ids left out of memory in the current context (a verification replay).
_EXCLUDED: ContextVar[frozenset[str]] = ContextVar("excluded_rules", default=frozenset())


@contextmanager
def excluding(rid: str):
    """Leave one learned rule out of every agent's memory within this context."""
    token = _EXCLUDED.set(_EXCLUDED.get() | {rid} if rid else _EXCLUDED.get())
    try:
        yield
    finally:
        _EXCLUDED.reset(token)


def load_learned_rules(skill_name: str) -> str:
    """A skill's learned rules, one bullet per line ("" if none)."""
    excluded = _EXCLUDED.get()
    return "\n".join(r.raw for r in parse_learned(skill_name) if r.id not in excluded)


def _quality_gate(rule: str) -> str | None:
    """A rejection reason (length outliers, JSON/code or truncated
    fragments), or None if the rule passes."""
    rule = rule.strip()
    if len(rule) < _MIN_LEN:
        return f"規則太短（{len(rule)} < {_MIN_LEN} 字元）——要一句完整可執行的指令"
    if len(rule) > _MAX_LEN:
        return f"規則太長（{len(rule)} > {_MAX_LEN} 字元）——濃縮成一句"
    for frag in ('": "', "{\"", "\"}", "```"):
        if frag in rule:
            return "規則含 JSON/程式碼碎片——請用自然語言描述"
    if rule.count("`") % 2 != 0:
        return "反引號不成對——內容疑似被截斷"
    if rule[0] in ")]}>,.:;'\"":
        return "開頭是標點——內容疑似被截斷"
    return None


def count_rules(skill_name: str) -> int:
    return len(parse_learned(skill_name))


def write_learned_rule(skill_name: str, rule: str, reason: str, source: str,
                       session=None, invocation_id: str = "", cue: str = "") -> str:
    """Append a consented rule to the skill's learned.md. Returns a report.

    Raises ValueError on gate/limit/duplicate failures so tool-error feedback
    tells the model what to fix. Given `session`, the turns before
    `invocation_id` are kept as the rule's case.
    """
    if source not in SOURCES:
        raise ValueError(f"'{source}' 不是學習規則的來源；只接受 {IN_SESSION}（使用者在對話中同意）")
    if not (home().skills / skill_name / "SKILL.md").exists():
        available = sorted(d.name for d in home().skills.iterdir() if (d / "SKILL.md").exists())
        raise ValueError(f"skill '{skill_name}' 不存在。可用: {', '.join(available)}")

    rejection = _quality_gate(rule)
    if rejection:
        raise ValueError(prompts.text("runtime/learned_rules#rejected", reason=rejection))

    same = nearest(rule, [r.text for r in parse_learned(skill_name)] + _sop_lines(skill_name))
    if same:
        raise ValueError(prompts.text("runtime/learned_rules#duplicate", threshold=str(SIMILAR),
                                      same=same[:120]))

    if count_rules(skill_name) >= MAX_RULES:
        raise ValueError(prompts.text("runtime/learned_rules#at_capacity", skill=skill_name,
                                      max_rules=str(MAX_RULES), rules="\n".join(
                                          f"- {r.text}" for r in parse_learned(skill_name))))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Date, source and reason go on the bullet; the Learning page shows them.
    situation = f"，情境：{cue.strip()[:60]}" if cue.strip() else ""
    append_raw(skill_name, f"- {rule.strip()} _（{today}，{source}，依據：{reason.strip()[:80]}{situation}）_")

    n = count_rules(skill_name)
    logger.info("Learned rule written for '%s' (%d/%d): %s", skill_name, n, MAX_RULES, rule[:80])
    if session is not None:
        try:
            save_case(skill_name, rule.strip(), reason, session, invocation_id)
        except Exception:
            logger.warning("could not keep the case for a learned rule", exc_info=True)
    return (
        f"✅ 已寫入 {skill_name} 的學習規則（{n}/{MAX_RULES}）：\n> {rule.strip()}\n"
        "下一個 turn 起生效（動態注入，無需重啟）。"
    )


def update_rule(skill_name: str, rid: str, rule: str, reason: str) -> str:
    """Rewrite one rule in place (the console's edit). The same checks as a new
    rule apply, against every rule but this one. Returns the new id: a rule's
    id is its text, so its case moves with it, and its review verdicts start
    over — they were about the old wording."""
    old = _find(skill_name, rid)
    rejection = _quality_gate(rule)
    if rejection:
        raise ValueError(prompts.text("runtime/learned_rules#rejected", reason=rejection))
    others = [r.text for r in parse_learned(skill_name) if r.id != rid]
    same = nearest(rule, others + _sop_lines(skill_name))
    if same:
        raise ValueError(prompts.text("runtime/learned_rules#duplicate", threshold=str(SIMILAR),
                                      same=same[:120]))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    source = old.source if old.source != LEGACY_SOURCE else CONSOLE
    line = f"- {rule.strip()} _（{today}，{source}，依據：{reason.strip()[:80]}）_"
    path = _learned_path(skill_name)
    with path_lock(path):
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(old.raw, line, 1), encoding="utf-8")
    new_id = rule_id(rule.strip())
    case = cases_dir() / skill_name / f"{rid}.json"
    if case.exists() and new_id != rid:
        case.rename(cases_dir() / skill_name / f"{new_id}.json")
    return new_id


def append_raw(skill_name: str, line: str) -> None:
    """Append one bullet, as written, to the skill's learned.md."""
    path = _learned_path(skill_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path_lock(path):
        existing = path.read_text(encoding="utf-8") if path.exists() else LEARNED_HEADER + "\n"
        path.write_text(existing.rstrip() + "\n" + line.rstrip("\n") + "\n", encoding="utf-8")


def save_case(skill_name: str, rule: str, reason: str, session, invocation_id: str) -> Path:
    """Keep the turns that led to a rule: the last few before the turn that
    wrote it, as eval invocations (user line, final reply, tool calls)."""
    from meeseek.runtime.history import eval_turns

    turns = eval_turns(list(session.events))
    before = [turn for inv, turn in turns if inv != invocation_id][-_CASE_TURNS:]
    case = {
        "rule": rule, "skill": skill_name, "reason": reason,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": session.id, "user_id": session.user_id,
        "turns": before,
    }
    path = cases_dir() / skill_name / f"{rule_id(rule)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, ensure_ascii=False, indent=1), encoding="utf-8")
    try:   # and in the case library, whole, so the review replays the conversation (runtime/cases.py)
        from meeseek.runtime import cases

        cases.from_session(skill_name, list(session.events), invocation_id, origin=f"rule:{rule_id(rule)}",
                           reference=f"學到的規則：{rule}（{reason}）")
    except Exception:  # noqa: BLE001 — the rule's own case is kept all the same
        logger.warning("learned rule: no library case", exc_info=True)
    return path


def load_case(skill_name: str, rid: str) -> dict | None:
    path = cases_dir() / skill_name / f"{rid}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _find(skill_name: str, rid: str) -> LearnedRule:
    rule = next((r for r in parse_learned(skill_name) if r.id == rid), None)
    if rule is None:
        raise ValueError(f"'{skill_name}' 沒有 id 為 {rid} 的學習規則")
    return rule


def forget(skill_name: str, rid: str) -> None:
    """Remove one rule by id (the console's delete)."""
    rule = _find(skill_name, rid)
    path = _learned_path(skill_name)
    with path_lock(path):
        kept = [ln for ln in path.read_text(encoding="utf-8").splitlines(keepends=True)
                if ln.rstrip("\n") != rule.raw]
        path.write_text("".join(kept), encoding="utf-8")


def graduate(skill_name: str, rid: str) -> str:
    """Move a learned rule into the skill's SKILL.md and commit that file.

    The rule is appended under GRADUATED_HEADING, removed from learned.md, and
    SKILL.md alone is committed (not pushed) for review. Returns the commit sha.
    """
    import subprocess

    rule = _find(skill_name, rid)
    sop = home().skills / skill_name / "SKILL.md"
    text = sop.read_text(encoding="utf-8").rstrip("\n")
    if GRADUATED_HEADING not in text:
        text += f"\n\n{GRADUATED_HEADING}"
    sop.write_text(f"{text}\n- {rule.text}\n", encoding="utf-8")
    forget(skill_name, rid)

    msg = f"{skill_name} SOP: adopt a learned rule\n\n{rule.text}\n\nReason: {rule.reason}"
    subprocess.run(["git", "-C", str(home().skills), "commit", "-q", "-m", msg, "--", str(sop.resolve())],
                   check=True, capture_output=True, text=True)
    from meeseek.runtime import policy

    policy.reload()   # the agents take the new SKILL.md on their next turn, no restart
    return subprocess.run(["git", "-C", str(home().skills), "rev-parse", "--short", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


def remove_learned_rule(skill_name: str, keyword: str) -> str:
    """Remove learned rule(s) containing `keyword` — archived, so the Context
    page can restore them (consent is runtime/consent.py's)."""
    from meeseek.runtime.rule_review import archive

    if not _learned_path(skill_name).exists():
        return f"'{skill_name}' 沒有學習規則檔。"
    doomed = [r for r in parse_learned(skill_name) if keyword in r.raw]
    if not doomed:
        return f"找不到含「{keyword}」的規則。"
    for r in doomed:
        archive(skill_name, r, "使用者要求移除")
    return f"已移除 {len(doomed)} 條含「{keyword}」的規則（可在 Context 頁還原）。"


def make_propose_tool(skill_name: str | None):
    """The propose tool, in one of its two shapes.

    `skill_name` bound → a sub-agent writing its OWN rules (it cannot name
    another skill). `skill_name=None` → the orchestrator form, which takes the
    skill as an argument because the conversation may be about any skill.
    """
    if skill_name is None:
        def propose_sop_rule(skill: str, rule: str, reason: str, cue: str = "", tool_context=None) -> str:
            """把一條學習規則寫入指定 skill（下一輪起生效）。

            使用者糾正了你、或你發現下次一定要先做的步驟時：先用 `ask_user` 把規則全文
            給使用者確認（options：寫入／不要），同意後才呼叫。規則寫成一句未來可執行的
            指令（誰在什麼情況該做什麼），不是事件描述。工具失敗不是規則。

            Args:
                skill: 目標 skill 名（如 infra、code）
                rule: 一句完整可執行的規則（15-300 字元）
                reason: 依據（使用者的糾正或對話中的發現）
                cue: 什麼情況下適用（例如「開 Jira 單時」），用來挑重播的案例；可省略
            """
            return write_learned_rule(skill, rule, reason, source=IN_SESSION, cue=cue, **_origin(tool_context))

        return propose_sop_rule

    def propose_sop_rule(rule: str, reason: str, cue: str = "", tool_context=None) -> str:
        """把一條學習規則寫入本 skill（下一輪起生效）。

        使用者糾正了你、或你發現下次一定要先做的步驟時：先用 `ask_user` 把規則全文
        給使用者確認（options：寫入／不要），同意後才呼叫。規則寫成一句未來可執行的
        指令（誰在什麼情況該做什麼），不是事件描述。工具失敗不是規則。

        Args:
            rule: 一句完整、可執行的規則（15-300 字元，自然語言）
            reason: 為什麼需要這條規則（依據哪個失敗/糾正）
            cue: 什麼情況下適用（例如「開 Jira 單時」），用來挑重播的案例；可省略
        """
        return write_learned_rule(skill_name, rule, reason, source=IN_SESSION, cue=cue, **_origin(tool_context))

    return propose_sop_rule


def _origin(tool_context) -> dict:
    """The session a propose call came from, for the rule's case."""
    if tool_context is None:
        return {}
    return {"session": tool_context.session, "invocation_id": tool_context.invocation_id}
