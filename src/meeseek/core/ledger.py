"""What a turn did, read off its events — the facts the policies hold a reply to.

  * the write ledger: every write a turn made and how it ended
  * which failures a reply leaves out, and which promise it makes
  * whether the person agreed to a write that changes every later turn
  * the raw evidence a reply has to show
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from meeseek.core.events import Event, ModelStepped, ToolRan, UserSaid

# A reply that mentions any of these has said something failed.
FAILURE_TERMS = re.compile(
    r"失敗|未成功|沒有成功|沒成功|無法|未能|錯誤|error|failed|沒有權限|權限不足|拒絕|未通過|"
    r"沒有(寫入|建立|掛|留言|更新)|未(寫入|建立|掛|留言|更新)|尚未|沒掛|不是 ?Epic|非 ?Epic|not an Epic|"
    r"已達上限|達到?.{0,8}上限|必須先移除|不支援|未開放|did not succeed|didn't succeed",
    re.IGNORECASE,
)

# A reply that promises meeseek itself will do something later. Kept narrow:
# a missed promise is only a missed check, a false hit nags on an honest reply.
PROMISE_TERMS = re.compile(
    r"我們?[^\n。，;：!?]{0,6}(?:會|再)[^\n。，;：!?]{0,6}(?:回報|通知|告訴|更新|追蹤|跟進|回覆)"
    r"|(?:完成|做完|結束|好了)之?後[^\n。，;：!?]{0,8}(?:回報|通知|告訴|更新|回覆)"
    r"|(?:稍後|晚點|待會兒?)再?(?:回報|通知|更新|回覆)"
    r"|will (?:report back|follow up|let you know|update you|get back to you)"
    r"|I[’']ll (?:report back|follow up|check back|let you know|update you|get back to you)",
    re.IGNORECASE,
)

_BATCH_FAILED = re.compile(r"\b\d+ FAILED\b")
# Keys that say what a write was about, when its tool does not declare a target.
_TARGET_KEYS = ("issue_key", "issue_id", "mr_iid", "iid", "skill", "page_id", "channel")


@dataclass(slots=True)
class Entry:
    tool: str
    args: dict
    status: str = "pending"     # ok | failed | pending (no result yet)
    detail: str = ""            # the failure text, for the reply
    declared: tuple[str, ...] = ()

    @property
    def target(self) -> str:
        """What the write was about, so a later success can supersede a failure."""
        keys = self.declared or tuple(k for k in _TARGET_KEYS if self.args.get(k))
        if keys:
            return ",".join(f"{k}={self.args.get(k)}" for k in keys)
        return json.dumps(self.args, ensure_ascii=False, sort_keys=True, default=str)


def failure_of(result: Any) -> str:
    """The failure text of a tool result, or "" if it succeeded. A write whose
    outcome is unknown counts as one: the reply must not call it done."""
    if isinstance(result, str):
        head = result.lstrip()[:60].lower()
        return result[:300] if head.startswith(("error", "failed", "nothing was posted")) else ""
    if not isinstance(result, dict):
        return ""
    if result.get("harness_status") == "unknown":
        return str(result.get("result", "outcome unknown"))[:300]
    if result.get("tool_failed") or ("error" in result and "result" not in result):
        return str(result.get("error") or result.get("result") or "failed")[:300]
    content = result.get("content")
    if isinstance(content, list) and content:
        text = str(content[0].get("text", "")) if isinstance(content[0], dict) else ""
        if result.get("isError") or text.startswith("Error"):
            return text[:300]
    # Registry writes report failure in their text: "Failed to …", or a batch's
    # first line "N of M … posted; K FAILED".
    text = result.get("result")
    if isinstance(text, str):
        first = text.lstrip().split("\n", 1)[0]
        if first.startswith(("Failed", "Nothing was posted", "Error")) or _BATCH_FAILED.search(first):
            return text[:300]
    return ""


def ledger(events: Iterable[Event], turn: str, writes: Callable[[str], bool],
           target_of: Callable[[str], tuple[str, ...]] = lambda name: ()) -> list[Entry]:
    """Every write `turn` made, with how it ended, in call order."""
    entries: list[Entry] = []
    by_id: dict[str, Entry] = {}
    for e in events:
        if e.turn != turn:
            continue
        if isinstance(e, ModelStepped):
            for c in e.calls:
                if writes(c.name):
                    entry = Entry(tool=c.name, args=dict(c.args), declared=target_of(c.name))
                    entries.append(entry)
                    by_id[c.id] = entry
        elif isinstance(e, ToolRan) and e.call.id in by_id:
            entry = by_id.pop(e.call.id)
            detail = failure_of(e.result) or ("failed" if e.failed else "")
            entry.status, entry.detail = ("failed", detail) if detail else ("ok", "")
    return entries


def earlier_turns(events: Iterable[Event], turn: str, n: int = 2) -> list[str]:
    order: list[str] = []
    for e in events:
        if e.turn and e.turn != turn and e.turn not in order:
            order.append(e.turn)
    return order[-n:]


def unsettled_failures(entries: list[Entry]) -> list[Entry]:
    """Failed writes no later success of the same tool and target made good."""
    out: list[Entry] = []
    seen: set[tuple[str, str]] = set()
    for i, e in enumerate(entries):
        key = (e.tool, e.target)
        if e.status != "failed" or key in seen:
            continue
        if any(x.status == "ok" and (x.tool, x.target) == key for x in entries[i + 1:]):
            continue
        seen.add(key)
        out.append(e)
    return out


def unreported(entries: list[Entry], reply: str, earlier: list[Entry] = ()) -> list[Entry]:
    """The failures a reply leaves out. A reply that says something failed is
    trusted to have said which. Naming a failed write's target is not saying it
    failed ("Linked AB-17" when the link did not take). An earlier turn's
    failure counts only when the reply talks about its target."""
    if FAILURE_TERMS.search(reply):
        return []
    failures = unsettled_failures(entries)
    settled_now = {(e.tool, e.target) for e in entries if e.status == "ok"}
    for e in unsettled_failures(list(earlier)):
        if (e.tool, e.target) not in settled_now and _mentions(reply, e):
            failures.append(e)
    return failures


def _mentions(reply: str, entry: Entry) -> bool:
    values = [str(v) for v in entry.args.values() if isinstance(v, (str, int))]
    return any(len(v) >= 3 and v in reply for v in values)


def promise(reply: str) -> str:
    """The phrase in which a reply promises a later step, or ""."""
    m = PROMISE_TERMS.search(reply)
    return m.group(0) if m else ""


# --- consent ------------------------------------------------------------------

# The person asking for the write in their own words is the consent.
ASKED_FOR = re.compile(
    r"同意.{0,6}(寫|記)|寫(進|入|成|到).{0,10}(規則|SOP|sop|記憶)|記(住|下來|成規則)"
    r"|(加|存)(進|入|成).{0,6}(規則|記憶)|remember (this|that)"
    r"|(刪|移除|拿掉|去掉|忘(掉|記)).{0,8}(規則|SOP|sop)|forget (this|that) rule", re.IGNORECASE)
REFUSAL = re.compile(r"不要|不用|不需要|先不|別寫|算了|取消|^否|^no\b|^cancel", re.IGNORECASE)


def consented(events: list[Event], turn: str) -> bool:
    """Did the person ask for the write this turn, or answer an ask_user from
    this turn or the one before with something other than a refusal?"""
    if any(isinstance(e, UserSaid) and e.turn == turn and ASKED_FOR.search(e.text) and not REFUSAL.search(e.text)
           for e in events):
        return True
    recent = set(earlier_turns(events, turn, 1)) | {turn}
    asked_at = None
    for i, e in enumerate(events):
        if isinstance(e, ModelStepped) and e.turn in recent and any(c.name == "ask_user" for c in e.calls):
            asked_at = i
    if asked_at is None:
        return False
    for e in events[asked_at + 1:]:
        answer = None
        if isinstance(e, ToolRan) and e.call.name == "ask_user" and isinstance(e.result, dict) and "answer" in e.result:
            answer = str(e.result["answer"])
        elif isinstance(e, UserSaid):
            answer = e.text
        if answer is not None:
            return not REFUSAL.search(answer.strip())
    return False


# --- evidence -----------------------------------------------------------------

_BLOCK = re.compile(r"```[^\n]*\n.*?\n```", re.DOTALL)
_MAX_LINES = 25


def raw_block(events: Iterable[Event], turn: str, shows_raw: Callable[[str], bool]) -> str:
    """The first code block of the last raw-evidence result in `turn`, trimmed."""
    block = ""
    for e in events:
        if isinstance(e, ToolRan) and e.turn == turn and shows_raw(e.call.name):
            result = e.result.get("result") if isinstance(e.result, dict) else e.result
            if isinstance(result, str) and (m := _BLOCK.search(result)):
                block = m.group(0)
    if not block:
        return ""
    lines = block.splitlines()
    if len(lines) > _MAX_LINES + 2:
        lines = [*lines[:_MAX_LINES + 1], f"… ({len(lines) - _MAX_LINES - 2} more lines)", lines[-1]]
    return "\n".join(lines)
