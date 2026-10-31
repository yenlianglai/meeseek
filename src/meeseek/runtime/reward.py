"""How well a turn went, from what came of it — and per skill, how it is going.

A reward is in [-1, 1], or None when nothing was said about the turn: silence
is not success. Signals rank — what a person did outranks what a detector
inferred, which outranks a model's judgement, which outranks the harness's
own marks:

    explicit   👍 +1 / 👎 -1, a write approved +0.6, approved after edits +0.2,
               rejected -0.8, an item dismissed as misfiled -0.5, a reply copied +0.3
    implicit   your next message corrected it -0.6, or asked the same again -0.4
    judge      a fast model's 1–5 for a turn nobody labelled (blind: it never
               sees which policy produced the turn), (score - 3) / 2
    automatic  the harness's marks on the turn (runtime/trajectory.py): a failed
               write the reply skipped, a note it had to add, the tool budget
               run out, a cap hit, a write held for consent, an empty reply,
               failed tool calls

A model error or a transient retry is the infrastructure's failure, not the
policy's: counted as a failure rate, never in the reward. The judge earns its
place: scored also on turns a person labelled, it is trusted only while it
agrees with them (`judge_trusted`).
"""

from __future__ import annotations

import logging
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from meeseek.runtime import feedback, trajectory
from meeseek.runtime.feedback import learning_dir
from meeseek.runtime.store import JsonlStore

logger = logging.getLogger(__name__)


def scoreboard_store() -> JsonlStore:
    return JsonlStore(learning_dir() / "scoreboard.jsonl")


EXPLICIT = {"write_approved": 0.6, "write_edited": 0.2, "write_rejected": -0.8, "copy": 0.3}
MISFILED = "分類錯"
IMPLICIT = {"correction": -0.6, "reask": -0.4}
AUTOMATIC = {"reconcile": -0.3, "appended": -0.5, "grace": -0.4, "cap_hits": -0.3,
             "consent_held": -0.2, "empty_nudge": -0.3}
TOOL_ERROR, TOOL_ERROR_FLOOR = -0.1, -0.3
CONFIDENCE = {"explicit": 1.0, "implicit": 0.6, "judge": 0.4, "automatic": 0.5, "none": 0.0}
TRUST = 0.7                       # judge/human agreement below this and the judge is not used
TAGS = ("wrong_route", "missed_step", "wrong_fact", "ignored_rule", "didnt_answer", "too_long", "unsafe_write")


@dataclass
class Reward:
    value: float | None
    confidence: float
    tier: str
    tags: list[str] = field(default_factory=list)
    infra: bool = False


def _clip(x: float) -> float:
    return round(max(-1.0, min(1.0, x)), 3)


def _automatic(turn: dict) -> float | None:
    auto = turn.get("auto", {})
    parts = [AUTOMATIC[k] * min(1, auto.get(k, 0)) for k in AUTOMATIC if auto.get(k)]
    if auto.get("tool_errors"):
        parts.append(max(TOOL_ERROR_FLOOR, TOOL_ERROR * auto["tool_errors"]))
    return _clip(sum(parts)) if parts else None


def reward(turn: dict, outcomes: list[dict], *, judge_trusted: bool = True) -> Reward:
    """The reward of one turn from its record and the outcomes about it."""
    infra = bool(turn.get("auto", {}).get("model_error") or turn.get("retries"))
    tags = sorted({t for o in outcomes for t in o.get("tags", [])})
    outcomes = _last_thumb(outcomes)
    explicit = []
    for o in outcomes:
        k = o.get("kind")
        if k == "thumb":
            explicit.append(1.0 if o.get("value", 0) > 0 else -1.0)
        elif k == "dismissed" and o.get("reason") == MISFILED:
            explicit.append(-0.5)
        elif k in EXPLICIT:
            explicit.append(EXPLICIT[k])
    if explicit:
        return Reward(_clip(sum(explicit) / len(explicit)), CONFIDENCE["explicit"], "explicit", tags, infra)
    implicit = [IMPLICIT[o["kind"]] for o in outcomes if o.get("kind") in IMPLICIT]
    auto = _automatic(turn)
    if implicit:
        return Reward(_clip(min(implicit) + 0.25 * (auto or 0)), CONFIDENCE["implicit"], "implicit", tags, infra)
    judged = [o for o in outcomes if o.get("kind") == "judge" and o.get("score")]
    if judged and judge_trusted:
        return Reward(_clip((judged[-1]["score"] - 3) / 2), CONFIDENCE["judge"], "judge", tags, infra)
    if auto is not None:
        return Reward(auto, CONFIDENCE["automatic"], "automatic", tags, infra)
    return Reward(None, 0.0, "none", tags, infra)


def _last_thumb(outcomes: list[dict]) -> list[dict]:
    """A turn's latest 👍/👎 is the one that counts: a 👎 sent again with its
    note, or changed to a 👍, replaces the one before."""
    thumbs = [o for o in outcomes if o.get("kind") == "thumb"]
    return [o for o in outcomes if o.get("kind") != "thumb"] + thumbs[-1:]


def _by_inv(outcomes: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for o in outcomes:
        out[o.get("inv", "")].append(o)
    return out


def rewards(days: float = 28) -> list[tuple[dict, Reward]]:
    """Every recorded turn of the last `days` with its reward."""
    since = time.time() - days * 86400
    turns = trajectory.read(since)
    outs = _by_inv(feedback.read(since - 86400))
    trusted = judge_trusted()
    return [(t, reward(t, outs.get(t["inv"], []), judge_trusted=trusted)) for t in turns]


# --- scoreboards --------------------------------------------------------------------------

def _week(ts: float) -> str:
    return time.strftime("%G-W%V", time.localtime(ts))


def _row(turns: list[tuple[dict, Reward]], outcomes: dict[str, list[dict]]) -> dict:
    kinds = defaultdict(int)
    for t, _ in turns:
        for o in _last_thumb(outcomes.get(t["inv"], [])):
            kinds[o["kind"] if o["kind"] != "thumb" else ("thumbs_up" if o.get("value", 0) > 0 else "thumbs_down")] += 1
    labeled = [r for _, r in turns if r.value is not None]
    lat = sorted(t.get("latency_s", 0) for t, _ in turns)
    return {"n": len(turns), "labeled": len(labeled), "reward_sum": round(sum(r.value for r in labeled), 3),
            "thumbs_up": kinds["thumbs_up"], "thumbs_down": kinds["thumbs_down"],
            "approved": kinds["write_approved"], "edited": kinds["write_edited"], "rejected": kinds["write_rejected"],
            "corrections": kinds["correction"], "reasks": kinds["reask"],
            "policy_fail": sum(1 for _, r in turns if r.tier == "automatic" and (r.value or 0) < 0),
            "infra_fail": sum(1 for _, r in turns if r.infra),
            "cost_sum": round(sum(t.get("cost_usd", 0) for t, _ in turns), 4),
            "latency_p50": lat[len(lat) // 2] if lat else 0, "latency_p90": lat[int(len(lat) * 0.9)] if lat else 0}


def _merge(rows: list[dict]) -> dict:
    """Weekly rows summed (latency: the largest week's)."""
    out: dict = defaultdict(float)
    for r in rows:
        for k, v in r.items():
            if isinstance(v, (int, float)) and not k.startswith("latency"):
                out[k] += v
    if rows:
        top = max(rows, key=lambda r: r.get("n", 0))
        out["latency_p50"], out["latency_p90"] = top.get("latency_p50", 0), top.get("latency_p90", 0)
    return dict(out)


def scoreboard(days: float = 28, by: str = "skill") -> list[dict]:
    """Per skill (or per skill and policy bundle): how it is going."""
    since = time.time() - days * 86400
    pairs = rewards(days)
    outs = _by_inv(feedback.read(since - 86400))
    groups: dict[tuple, list] = defaultdict(list)
    for t, r in pairs:
        key = (t.get("skill", "router"),) + ((t.get("policy", {}).get("bundle", ""),) if by == "bundle" else ())
        groups[key].append((t, r))
    rows: dict[tuple, list[dict]] = defaultdict(list)
    for key, items in groups.items():
        rows[key].append(_row(items, outs))
    for old in scoreboard_store().read():
        if old.get("until", 0) >= since:
            key = (old["skill"],) + ((old.get("bundle", ""),) if by == "bundle" else ())
            rows[key].append(old)
    out = []
    for key, rs in sorted(rows.items()):
        m = _merge(rs)
        n, labeled = m.get("n", 0), m.get("labeled", 0)
        writes = m.get("approved", 0) + m.get("edited", 0) + m.get("rejected", 0)
        out.append({"skill": key[0], **({"bundle": key[1]} if by == "bundle" else {}), "turns": int(n),
                    "labeled_share": round(labeled / n, 2) if n else 0,
                    "reward": round(m.get("reward_sum", 0) / labeled, 2) if labeled else None,
                    "approval_rate": round(m.get("approved", 0) / writes, 2) if writes else None,
                    "edit_rate": round(m.get("edited", 0) / writes, 2) if writes else None,
                    "correction_rate": round(m.get("corrections", 0) / n, 2) if n else 0,
                    "reask_rate": round(m.get("reasks", 0) / n, 2) if n else 0,
                    "failure_rate": round(m.get("policy_fail", 0) / n, 2) if n else 0,
                    "infra_failure_rate": round(m.get("infra_fail", 0) / n, 2) if n else 0,
                    "thumbs": [int(m.get("thumbs_up", 0)), int(m.get("thumbs_down", 0))],
                    "cost_per_turn": round(m.get("cost_sum", 0) / n, 4) if n else 0,
                    "latency_p50": m.get("latency_p50", 0), "latency_p90": m.get("latency_p90", 0)})
    return out


def compact(now: float | None = None) -> int:
    """Fold turns older than trajectory.RETAIN_DAYS into weekly rows per skill
    and policy, and drop them and their outcomes; how many turns went."""
    now = now or time.time()
    cutoff = now - trajectory.RETAIN_DAYS * 86400
    turns = trajectory.fold(trajectory.turns_store().read())
    old = [t for t in turns if t.get("at", now) < cutoff]
    if not old:
        return 0
    outs = _by_inv(feedback.outcomes_store().read())
    trusted = judge_trusted()
    groups: dict[tuple, list] = defaultdict(list)
    for t in old:
        r = reward(t, outs.get(t["inv"], []), judge_trusted=trusted)
        groups[(t.get("skill", "router"), t.get("policy", {}).get("bundle", ""), _week(t["at"]))].append((t, r))
    for (skill, bundle, week), items in groups.items():
        scoreboard_store().append({"skill": skill, "bundle": bundle, "week": week,
                           "until": max(t["at"] for t, _ in items), **_row(items, outs)})
    gone = {t["inv"] for t in old}
    trajectory.turns_store().update(lambda rs: [r for r in rs if r.get("inv") not in gone])
    feedback.outcomes_store().update(lambda rs: [o for o in rs if o.get("inv") not in gone])
    return len(gone)


# --- the judge, where nobody said anything ------------------------------------------------

class _Judged(BaseModel):
    score: int = Field(ge=1, le=5, description="1 很差 … 5 很好")
    tags: list[str] = Field(default_factory=list, description="問題標籤，只能用給定的")
    why: str = Field(description="一句話理由")


def rubric(skill: str) -> str:
    """The skill's rubric (skills/<name>/rubric.md, written by a person; no
    agent reads it), or the default one."""
    from meeseek.config.paths import home
    from meeseek.context import prompts

    p = home().skills / skill / "rubric.md"
    return p.read_text(encoding="utf-8") if p.exists() else prompts.text("internal/turn_judge#default_rubric")


def judge(turn: dict) -> dict | None:
    """A fast model's blind score for one turn; None when it could not judge."""
    from meeseek.config import cfg
    from meeseek.context import prompts
    from meeseek.runtime.oneshot import ask

    calls = ", ".join(f"{n}{'（失敗）' if failed else ''}" for n, failed in turn.get("calls", [])) or "（沒有）"
    try:
        j = ask(prompts.text("internal/turn_judge#judge", rubric=rubric(turn.get("skill", "router")),
                             request=turn.get("request", "")[:3000], calls=calls, reply=turn.get("reply_head", "")),
                model=cfg.models.fast, schema=_Judged, temperature=0, max_output_tokens=600, timeout_s=120,
                source="rl_judge")
        return {"score": j.score, "tags": [t for t in j.tags if t in TAGS], "why": j.why[:300]}
    except Exception:  # noqa: BLE001 — no score is better than a made-up one
        logger.warning("reward: judge failed for %s", turn.get("inv"), exc_info=True)
        return None


def judge_unlabeled(limit: int = 30, calibrate: int = 5, spend=None) -> int:
    """Score up to `limit` recent turns nobody labelled — the skills with the
    fewest labels first — and `calibrate` that a person did label, to keep the
    judge honest. `spend(usd)` books each call against a budget and says
    whether there was room. How many turns were scored."""
    pairs = rewards(14)
    outs = _by_inv(feedback.read(time.time() - 15 * 86400))
    judged = {inv for inv, os in outs.items() if any(o["kind"] == "judge" for o in os)}
    unlabeled = [t for t, r in pairs if r.tier in ("none", "automatic") and t["inv"] not in judged
                 and t.get("request")]
    labeled_by_skill = defaultdict(int)
    for t, r in pairs:
        labeled_by_skill[t.get("skill")] += r.tier in ("explicit", "implicit")
    unlabeled.sort(key=lambda t: (labeled_by_skill[t.get("skill")], -t.get("at", 0)))
    humans = [t for t, r in pairs if r.tier == "explicit" and t["inv"] not in judged and t.get("request")]
    n = 0
    for t in unlabeled[:limit] + humans[:calibrate]:
        if spend is not None and not spend(0.006):
            break
        j = judge(t)
        if j:
            feedback.record(t["inv"], t.get("session", ""), "judge", by="judge", **j)
            n += 1
    return n


def judge_trusted() -> bool:
    """Whether the judge agrees with people often enough to count (on the turns
    both scored); trusted until there are five to compare."""
    outs = _by_inv(feedback.read(time.time() - 90 * 86400))
    pairs = []
    for os in outs.values():
        human = [o for o in os if o["kind"] == "thumb"]
        judged = [o for o in os if o["kind"] == "judge" and o.get("score")]
        if human and judged:
            pairs.append((human[-1].get("value", 0) > 0, judged[-1]["score"] >= 3))
    if len(pairs) < 5:
        return True
    return statistics.mean(a == b for a, b in pairs) >= TRUST
