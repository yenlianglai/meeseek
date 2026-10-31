"""What came of a turn: the signals a person gave, said or implied about it.

Every outcome is one line in .cache/learning/outcomes.jsonl, tied to the
invocation it is about (runtime/trajectory.py records the turn itself):

    thumb            👍/👎 on a reply, console or phone, with an optional note
    copy             the reply or its draft copied
    write_approved   a held write approved as the agent proposed it
    write_edited     approved after your edits — the key-level diff kept
    write_rejected   rejected, with the note
    dismissed        an Inbox item set aside, with the reason
    correction       your next message corrected it ("不對", "我是說…")
    reask            your next message asked the same again
    judge            a fast model's score where no person said anything (runtime/reward.py)

Only the first two come in over HTTP (`HUMAN_KINDS`); the Inbox records its own
as it decides, so an item pruned after 30 days keeps its outcomes. The
correction and re-ask detectors call no model. Append-only: the server and the
weekly run (runtime/improve.py) both write here, from two processes.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from meeseek.config.paths import home
from meeseek.runtime.store import JsonlStore


def learning_dir() -> Path:
    return home().data / "learning"


def outcomes_store() -> JsonlStore:
    return JsonlStore(learning_dir() / "outcomes.jsonl")


HUMAN_KINDS = frozenset({"thumb", "copy"})      # what the feedback endpoint takes
KINDS = HUMAN_KINDS | {"write_approved", "write_edited", "write_rejected", "dismissed",
                       "correction", "reask", "judge"}

_CORRECTION = re.compile(
    r"(不對|不是這|錯了|你錯|我是說|我要的是|應該是|重來|重新來|搞錯|看錯|沒有要|不是要|"
    r"\bwrong\b|that'?s not|\bI meant\b|not what I)", re.I)
_BARE_ASK = re.compile(r"^\s*(\?|？|然後呢|所以呢|嗯\?|？？|\?\?)\s*$")
REASK_WINDOW_S = 15 * 60


def record(inv: str, session: str, kind: str, *, by: str = "you", **fields) -> None:
    """Append one outcome. Never raises: a lost signal must not fail a turn."""
    if kind not in KINDS or not inv:
        return
    outcomes_store().append({"inv": inv, "session": session, "kind": kind, "by": by, "at": time.time(),
                     **{k: v for k, v in fields.items() if v not in (None, "")}})


def read(since: float = 0) -> list[dict]:
    return [o for o in outcomes_store().read() if o.get("at", 0) >= since]


def is_correction(text: str) -> bool:
    return bool(_CORRECTION.search(text or ""))


def is_reask(prev_request: str, text: str, gap_s: float) -> bool:
    """The same thing asked again soon after, or a bare "?" — the reply did not land."""
    from meeseek.runtime.learned_rules import SIMILAR, similarity

    if _BARE_ASK.match(text or ""):
        return True
    return gap_s <= REASK_WINDOW_S and bool(prev_request) and similarity(prev_request, text) >= SIMILAR


def implicit(text: str, *, prev_request: str, gap_s: float, waiting: bool) -> str:
    """What a new message says about the turn before it: "correction", "reask"
    or "". An answer to an open ask_user question, a slash command and
    meeseek's own nudges say nothing about the reply."""
    from meeseek.context import prompts

    t = (text or "").strip()
    if not t or waiting or t.startswith("/"):
        return ""
    if t.startswith(prompts.source("runtime/callbacks#empty_reply").strip()[:12]):
        return ""
    if is_correction(t):
        return "correction"
    if is_reask(prev_request, t, gap_s):
        return "reask"
    return ""


def arg_diff(proposed: dict, approved: dict) -> dict:
    """Key by key, what you changed in a write before approving it."""
    out = {}
    for k in sorted(set(proposed) | set(approved)):
        a, b = proposed.get(k), approved.get(k)
        if a != b:
            out[k] = {"from": str(a)[:400] if a is not None else None, "to": str(b)[:400] if b is not None else None}
    return out
