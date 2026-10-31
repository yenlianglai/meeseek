"""The Inbox — events that started work on their own, and the gate on what
that work may do.

A trigger (runtime/triggers.py) files an item (a mail, chat message, alert,
meeting). One that needs you gets an orchestrator turn in its own INBOX_USER
session, which you can open in Chat. There the agent only reads and drafts:

  * a reply is a draft in a ```reply block you send yourself — the Workspace
    credential stays read-only;
  * every write is held by the approval plugin as a pending action. Once you
    approve it on the Inbox page, the next turn lets exactly that call
    through; the agent's later writes wait again.

Items are one JSON file each under items_dir().
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path

from meeseek.app import app
from meeseek.config.paths import home
from meeseek.context import prompts
from meeseek.runtime.store import path_lock

logger = logging.getLogger(__name__)

INBOX_USER = "inbox"


def items_dir() -> Path:
    return home().data / "inbox"


KEEP_DAYS = 30
_MAX_PARALLEL = 2
_RESULT_CHARS = 1500
_REPLY_BLOCK = re.compile(r"```reply[^\n]*\n(.*?)```", re.S)

# Statuses an item is settled in: nothing more happens to it.
SETTLED = ("done", "dismissed", "ignored", "ignore", "fyi", "failed", "expired")
# Statuses an item waits on you in.
WAITING = ("ready", "needs_approval", "needs_input")


async def _after_turn(item_id: str) -> None:
    item = get(item_id)
    for fn in list(app().after_inbox_turn):
        try:
            await fn(item)
        except Exception:  # noqa: BLE001 — a delivery failure must not fail the turn
            logger.exception("[inbox] after-turn hook failed on %s", item_id)


def _path(item_id: str) -> Path:
    return items_dir() / f"{re.sub(r'[^a-f0-9]', '', item_id)}.json"


def save(item: dict) -> None:
    items_dir().mkdir(parents=True, exist_ok=True)
    with path_lock(_path(item["id"])):
        item["updated_at"] = time.time()
        _path(item["id"]).write_text(json.dumps(item, ensure_ascii=False, indent=1), encoding="utf-8")


def get(item_id: str) -> dict | None:
    p = _path(item_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _all() -> list[dict]:
    if not items_dir().exists():
        return []
    out = []
    for p in items_dir().glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue  # half-written; the next read sees it whole
    return out


def items(limit: int = 100) -> list[dict]:
    """Newest first."""
    return sorted(_all(), key=lambda i: i.get("received_at") or i["created_at"], reverse=True)[:limit]


def by_key(key: str) -> dict | None:
    """The item a message was filed as: its first, or one merged into it since."""
    return next((i for i in _all() if i.get("key") == key or key in i.get("keys", ())), None)


# --- one item per conversation --------------------------------------------------
# A source names what one conversation is — a Chat space, a mail thread, an MR —
# as an item's `group`: the next message there updates that item rather than
# filing another, and your own reply there settles it.

def group_of(item: dict) -> str:
    """The item's conversation; for an item filed before items said so, by its meta."""
    if item.get("group"):
        return item["group"]
    meta, kind = item.get("meta") or {}, kind_of(item)
    if kind == "chat" and meta.get("space"):
        return meta["space"]
    if kind == "mail" and meta.get("thread_id"):
        return f"thread:{meta['thread_id']}"
    return ""


def by_group(group: str) -> list[dict]:
    """The conversation's items, newest first."""
    if not group:
        return []
    return sorted((i for i in _all() if group_of(i) == group),
                  key=lambda i: i.get("received_at") or i["created_at"], reverse=True)


MERGED_BODY_CHARS = 6000


def merge(item: dict, message: dict) -> dict:
    """A new message in the item's conversation, added to it: the item now
    shows the latest, with what came before above it (the oldest falls off)."""
    when = time.strftime("%m/%d %H:%M", time.localtime(message.get("received_at") or time.time()))
    added = f"— {message.get('sender') or '?'} · {when}\n{message.get('body', '')}"
    body = f"{item['body']}\n\n{added}" if item.get("body") else added
    item["body"] = body[-MERGED_BODY_CHARS:]
    item["keys"] = [*item.get("keys", []), message["key"]]
    for k in ("title", "sender", "link"):
        if message.get(k):
            item[k] = message[k]
    notice_only = (item.get("meta") or {}).get("notice")
    item["meta"] = {**item.get("meta", {}), **(message.get("meta") or {})}
    if notice_only and not (message.get("meta") or {}).get("notice"):
        # Filed from notification mail only: the ticket's or MR's own source
        # now speaks for it, so the item is what that source says it is.
        item["meta"].pop("notice", None)
        item["kind"], item["source"] = message.get("kind") or item.get("kind", ""), message.get("source") or item["source"]
    item["received_at"] = message.get("received_at") or time.time()
    item["seen"] = False
    return item


def fold(item: dict, notice: dict) -> dict:
    """A notification mail about the item's ticket or MR: kept with the item,
    out of sight. It asks nothing (the ticket's or MR's own source says what
    does), so the item's status, title and whether you saw it stay as they are."""
    when = time.strftime("%m/%d %H:%M", time.localtime(notice.get("received_at") or time.time()))
    added = f"— {notice.get('sender') or '?'} · {when}（通知信）{notice.get('title', '')}"
    item["body"] = (f"{item['body']}\n\n{added}" if item.get("body") else added)[-MERGED_BODY_CHARS:]
    item["keys"] = [*item.get("keys", []), notice["key"]]
    item["notices"] = item.get("notices", 0) + 1
    save(item)
    return item


def answered(group: str, at: float) -> int:
    """You wrote in the conversation at `at`: what was waiting on you there
    from before is settled. A turn still running is left to finish."""
    n = 0
    for item in by_group(group):
        if item["status"] in SETTLED or item["status"] in ("working", "new", "needs_approval"):
            continue
        if (item.get("received_at") or 0) <= at:
            item["status"] = "done"
            item["triage"] = {**item.get("triage", {}), "settled_by": "you replied"}
            save(item)
            n += 1
    return n


# --- what goes stale ------------------------------------------------------------

STALE_APPROVAL_S = 3 * 86400   # a held write waits this long, then the digest says so once
EXPIRE_AFTER_NOTE_S = 86400    # and after that mention, this long before it expires


def _ended(iso: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def _held_at(item: dict) -> float:
    return max((a.get("at") or 0 for a in item.get("actions", []) if a["status"] == "pending"), default=0.0)


def stale_approvals(now: float | None = None) -> list[dict]:
    """Items whose held writes have waited STALE_APPROVAL_S, not yet named in a digest."""
    now = now or time.time()
    return [i for i in _all() if i["status"] == "needs_approval" and not i["meta"].get("stale_noted")
            and 0 < _held_at(i) <= now - STALE_APPROVAL_S]


def note_stale(item_ids: list[str], now: float | None = None) -> None:
    """The digest named these: the next expiry pass a day on lets them go."""
    for item_id in item_ids:
        if item := get(item_id):
            item["meta"]["stale_noted"] = now or time.time()
            save(item)


def _expire(item: dict, why: str, status: str) -> None:
    item["status"] = status
    item["triage"] = {**item.get("triage", {}), "settled_by": why}
    for a in item["actions"]:
        if a["status"] == "pending":
            a["status"] = "expired"
    save(item)


def expire(now: float | None = None, *, digest: bool = True) -> int:
    """Settle what nobody will act on any more: a meeting's brief once the
    meeting has ended (done), and a held write the digest named a day ago
    (expired). With no digest to name it (`digest` False), a held write
    expires once it is stale."""
    now = now or time.time()
    n = 0
    for item in _all():
        meta = item.get("meta") or {}
        brief = kind_of(item) == "meeting" and isinstance(meta.get("end"), str)
        if brief and item["status"] in WAITING and 0 < _ended(meta["end"]) <= now:
            _expire(item, "the meeting ended", "done")
            n += 1
        elif item["status"] == "needs_approval" and 0 < _held_at(item) <= now - STALE_APPROVAL_S:
            noted = meta.get("stale_noted") or 0
            if (noted and noted <= now - EXPIRE_AFTER_NOTE_S) or not digest:
                _expire(item, f"no answer in {STALE_APPROVAL_S // 86400} days", "expired")
                n += 1
    return n


def by_session(session_id: str) -> dict | None:
    return next((i for i in _all() if i.get("session_id") == session_id), None)


def file(*, source: str, key: str, title: str, sender: str = "", link: str = "",
         body: str = "", received_at: float | None = None, meta: dict | None = None,
         kind: str = "", group: str = "") -> dict | None:
    """File a new item; None if one with this key exists already. `message` is
    set by whoever starts its turn."""
    if by_key(key):
        return None
    now = time.time()
    item = {
        "id": uuid.uuid4().hex[:10], "source": source, "kind": kind or kind_of({"source": source}),
        "key": key, "group": group, "title": title,
        "sender": sender, "link": link, "body": body, "meta": meta or {},
        "received_at": received_at or now, "created_at": now, "updated_at": now,
        "status": "new", "triage": {}, "session_id": "", "user_id": INBOX_USER,
        "message": "", "reply": "", "draft": "", "question": "", "actions": [], "seen": False,
        "error": "",
    }
    save(item)
    return item


# What an item is, for an item filed before items said so: by where it came from.
_KIND_OF_SOURCE = {"gmail": "mail", "chat": "chat", "alert": "alert", "calendar": "meeting"}


def kind_of(item: dict) -> str:
    """mail | chat | alert | meeting | … — what meeseek does with the item."""
    return item.get("kind") or _KIND_OF_SOURCE.get(item.get("source", ""), item.get("source", ""))


def prune(now: float | None = None) -> int:
    """Drop settled items older than KEEP_DAYS."""
    cutoff = (now or time.time()) - KEEP_DAYS * 86400
    gone = 0
    for item in _all():
        if item["status"] in SETTLED and item["updated_at"] < cutoff:
            _path(item["id"]).unlink(missing_ok=True)
            gone += 1
    return gone


def mark_seen(item_id: str) -> None:
    """You opened the item. A meeting's brief or notes asks nothing of you:
    read, it is done."""
    item = get(item_id)
    if not item:
        return
    done = kind_of(item) == "meeting" and item["status"] == "ready"
    if item["seen"] and not done:
        return
    item["seen"] = True
    if done:
        item["status"] = "done"
    save(item)


def dismiss(item_id: str, reason: str = "") -> None:
    """Set an item aside; the reason, if you gave one, is a signal about the
    turn that handled it (runtime/feedback.py)."""
    item = _must(item_id)
    item["status"] = "dismissed"
    if reason:
        item["dismiss_reason"] = reason
    _record_dismissal(item, reason)
    for a in item["actions"]:
        if a["status"] == "pending":
            a["status"] = "rejected"
    save(item)


def _record_dismissal(item: dict, reason: str) -> None:
    from meeseek.runtime import feedback

    invs = [a.get("invocation_id") for a in item.get("actions", []) if a.get("invocation_id")]
    inv = invs[-1] if invs else last_invocation(item.get("session_id", ""))
    feedback.record(inv, item.get("session_id", ""), "dismissed", reason=reason, source=item.get("source", ""))


def last_invocation(session_id: str) -> str:
    """The last turn of an item's session, read straight from the store."""
    if not session_id:
        return ""
    try:
        from meeseek.runtime import history

        found = history.rows(session=session_id)
        return found[-1].turn if found else ""
    except Exception:  # noqa: BLE001
        return ""


def _must(item_id: str) -> dict:
    item = get(item_id)
    if item is None:
        raise ValueError(f"no such Inbox item: {item_id}")
    return item


# --- approvals ------------------------------------------------------------------

def _canonical(args: dict) -> str:
    return json.dumps({k: v for k, v in (args or {}).items() if v is not None},
                      ensure_ascii=False, sort_keys=True, default=str)


def hold(item: dict, tool: str, args: dict, agent: str, invocation_id: str = "") -> dict:
    """File a write the agent tried as a pending action. A repeat of one
    already pending is the same action. The turn that tried it is kept, so your
    decision is a signal about that turn (runtime/feedback.py)."""
    same = next((a for a in item["actions"] if a["status"] == "pending" and a["tool"] == tool
                 and _canonical(a["args"]) == _canonical(args)), None)
    if same:
        return same
    action = {"id": uuid.uuid4().hex[:8], "tool": tool, "args": args, "agent": agent,
              "status": "pending", "result": "", "at": time.time(), "invocation_id": invocation_id}
    item["actions"].append(action)
    save(item)
    return action


def take_approval(item: dict, tool: str, args: dict) -> dict | None:
    """The approved action this call carries out, marked running; None if the
    call is not exactly what was approved."""
    for a in item["actions"]:
        if a["status"] == "approved" and a["tool"] == tool and _canonical(a["args"]) == _canonical(args):
            a["status"] = "running"
            save(item)
            return a
    return None


def settle(item: dict, tool: str, result) -> None:
    """Record what a running approved action returned."""
    from meeseek.runtime.results import tool_failed

    action = next((a for a in item["actions"] if a["status"] == "running" and a["tool"] == tool), None)
    if action is None:
        return
    action["status"] = "failed" if tool_failed(result) else "done"
    text = result.get("result", result) if isinstance(result, dict) else result
    action["result"] = str(text)[:_RESULT_CHARS]
    save(item)


# --- the agent's turn -----------------------------------------------------------

def _draft(reply: str) -> str:
    blocks = _REPLY_BLOCK.findall(reply or "")
    return blocks[-1].strip() if blocks else ""


async def work(item_id: str, message: str = "") -> None:
    """One orchestrator turn on an item's session; the item records the outcome.
    Empty `message` re-runs the item's last one (a retry). A daily review's
    item instead starts the fixes you approved (runtime/daily_review.py)."""
    from meeseek.runtime.mcp_server import run_orchestrator_turn

    if kind_of(get(item_id) or {}) == "review":
        # The daily review's fixes run as Claude sessions, not as an agent turn.
        from meeseek.runtime import daily_review

        await daily_review.carry_out(item_id)
        return
    a = app()
    a.inbox_slots = a.inbox_slots or asyncio.Semaphore(_MAX_PARALLEL)
    async with a.inbox_slots:
        item = _must(item_id)
        item["session_id"] = item["session_id"] or f"inbox-{item['id']}"
        item["message"] = message or item["message"]
        item["status"], item["error"] = "working", ""
        save(item)
        try:
            reply, _, needs = await run_orchestrator_turn(item["message"], item["session_id"],
                                                          user_id=INBOX_USER)
        except Exception as e:  # noqa: BLE001 — a failed turn is an item state, not a crash
            logger.exception("[inbox] turn on %s failed", item_id)
            item = _must(item_id)
            from meeseek.runtime.llm import failure_message
            item["status"], item["error"] = "failed", failure_message(e)
            save(item)
            await _after_turn(item_id)
            return
        item = _must(item_id)
        if _model_failed(reply) and not item["meta"].get("retried"):
            # A model error no retry inside the turn got past (a stray 400 from
            # Vertex): nobody is watching an Inbox turn, so try once more, a
            # minute later, from a fresh session — the failed one ends mid-turn.
            # The retry is a wake on disk, not a timer: it survives a restart.
            from meeseek.runtime import wakes

            item["meta"]["retried"] = True
            item["session_id"] = f"inbox-{item['id']}-r1"
            item["status"] = "working"
            save(item)
            wakes.file(kind="inbox_retry", due_at=time.time() + _RETRY_AFTER_S,
                       payload={"item_id": item_id}, session_id=item["session_id"],
                       why="模型錯誤；一分鐘後從新 session 重試一次",
                       key=f"inbox_retry:{item_id}")
            return
        item["reply"] = reply
        item["draft"] = _draft(reply) or item["draft"]
        item["question"] = needs[0] if needs else ""
        if any(a["status"] == "pending" for a in item["actions"]):
            item["status"] = "needs_approval"
        elif needs:
            item["status"] = "needs_input"
        else:
            item["status"] = "ready"
        item["seen"] = False
        save(item)
    await _after_turn(item_id)


_RETRY_AFTER_S = 60


def _model_failed(reply: str) -> bool:
    """The reply is the model-error notice (runtime/callbacks#model_error)."""
    head = prompts.text("runtime/callbacks#model_error", detail="").split("：")[0]
    return reply.strip().startswith(head)


def start(item_id: str, message: str = "") -> asyncio.Task:
    """Run `work` in the background on the running loop."""
    return asyncio.get_running_loop().create_task(work(item_id, message))


def approve(item_id: str, action_id: str, args: dict | None = None) -> str:
    """Approve one pending action, with your edits to its arguments; returns
    the message that tells the agent to go ahead."""
    item = _must(item_id)
    action = next((a for a in item["actions"] if a["id"] == action_id), None)
    if action is None or action["status"] != "pending":
        raise ValueError("this action is not waiting for approval")
    from meeseek.runtime import feedback

    proposed = dict(action["args"])
    if args is not None and args != proposed:
        action["proposed_args"] = proposed     # what the agent wrote, before your edits
        action["args"] = args
        feedback.record(action.get("invocation_id", ""), item.get("session_id", ""), "write_edited",
                        tool=action["tool"], diff=feedback.arg_diff(proposed, args))
    else:
        feedback.record(action.get("invocation_id", ""), item.get("session_id", ""), "write_approved",
                        tool=action["tool"])
    action["status"] = "approved"
    save(item)
    return prompts.text("runtime/inbox#approved", tool=action["tool"],
                        args=json.dumps(action["args"], ensure_ascii=False, indent=1))


def reject(item_id: str, action_id: str, note: str = "") -> str:
    item = _must(item_id)
    action = next((a for a in item["actions"] if a["id"] == action_id), None)
    if action is None or action["status"] != "pending":
        raise ValueError("this action is not waiting for approval")
    action["status"], action["result"] = "rejected", note
    from meeseek.runtime import feedback

    feedback.record(action.get("invocation_id", ""), item.get("session_id", ""), "write_rejected",
                    tool=action["tool"], note=note)
    if not any(a["status"] == "pending" for a in item["actions"]):
        item["status"] = "ready"
    save(item)
    return prompts.text("runtime/inbox#rejected", tool=action["tool"], note=note or "（沒有說明）")


def recover() -> int:
    """Items a restart left mid-turn: marked failed, so the page offers a
    retry. One whose retry is already filed as a wake is left alone — the wake
    survived the restart and will run it."""
    from meeseek.runtime import wakes

    n = 0
    for item in _all():
        if item["status"] == "working" or (item["status"] == "new" and item["message"]):
            if wakes.has_pending(f"inbox_retry:{item['id']}"):
                continue
            item["status"], item["error"] = "failed", "Interrupted by a server restart"
            save(item)
            n += 1
    return n
