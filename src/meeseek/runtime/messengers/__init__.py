"""Your phone as a way in: talk to the orchestrator from Telegram.

A phone message is an Inbox item (runtime/inbox.py) with its own session, so
every write it wants is held until you approve it. The answer goes back to the
chat after every turn, and each held write arrives there with Approve / Reject
buttons; the Inbox page works too, and its turns are delivered to the phone.

Messages in a chat continue one conversation (same item and session) until
`/new` or IDLE_S of quiet. A message arriving mid-turn is queued behind it.

Only user ids in project.yaml (`messengers.*_allowed_users`) are answered.
With none listed, the bot replies to anyone with just their own id, so you
can add yours.

`triggers.quiet_hours` ("23-8", local) holds the pushes the bot starts itself
— proposals, coach rehearsals and reports, Claude results — until the hours
end: deferred as a wake (runtime/wakes.py) or left to the next cron, never
dropped. A reply to a message you just sent (deliver) goes out regardless.

An adapter (telegram.py, long polling) turns platform events into on_text /
on_choice and implements Channel. start() brings up the ones project.yaml
turns on; their health shows on the Inbox page as sources.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from meeseek.app import app
from meeseek.config.paths import home
from meeseek.context import prompts
from meeseek.runtime import inbox

logger = logging.getLogger(__name__)

IDLE_S = 8 * 3600      # a chat quiet this long starts a new conversation
CHUNK = 3800           # characters per chat message (Telegram's limit is 4096)
_ARGS_CHARS = 1200

HELP = ("直接傳訊息給我就好，我會接著同一段對話做。\n"
        "/new — 開新的對話\n"
        "/claude — 列出 Claude Code 的 session；/claude 2 指定之後的任務交給第 2 個（/claude new 開新的、/claude off 取消）\n"
        "需要寫入的動作（開 Jira、MR 留言…）會先問你，按「同意」才執行。")


class Channel(Protocol):
    name: str            # the item's source, e.g. "telegram"
    label: str           # how the agent is told where you are
    typing_every: float  # seconds a typing indicator lasts

    def allowed(self, user: str) -> bool: ...
    def configured(self) -> bool: ...          # any user ids listed at all
    async def send(self, chat: str, texts: list[str]) -> None: ...              # notices, as plain text
    # The agent's answer, with the files it made; returns the files it already
    # delivered along with the text (a photo captioned by the answer).
    async def send_reply(self, chat: str, markdown: str, files: list[Path]) -> list[Path]: ...
    async def send_files(self, chat: str, paths: list[Path]) -> None: ...       # the rest
    async def ask(self, chat: str, item: dict, action: dict) -> None: ...
    async def typing(self, chat: str) -> None: ...


def channels() -> dict[str, Channel]:
    return app().channels


def register(channel: Channel) -> None:
    a = app()
    a.channels[channel.name] = channel
    for hooks, fn in ((a.after_inbox_turn, deliver), (a.after_inbox_turn, coach_push),
                      (a.after_claude_turn, claude_done)):
        if fn not in hooks:
            hooks.append(fn)


def set_status(name: str, ok: bool, error: str = "") -> None:
    app().channel_status[name] = {"ok": ok, "error": error, "at": time.time(), "filed": 0}


# --- text ------------------------------------------------------------------------

_FENCE = re.compile(r"^```[^\n]*\n?", re.M)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def plain(markdown: str) -> str:
    """Markdown as a chat shows it: no fences, headings, bold or link syntax."""
    text = _FENCE.sub("", markdown or "")
    text = _LINK.sub(lambda m: m[1] if m[1] == m[2] else f"{m[1]} {m[2]}", text)
    text = re.sub(r"^#{1,6}\s+(.*)$", r"【\1】", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m[1] or m[2], text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _blocks(markdown: str) -> list[str]:
    """Paragraphs, with a fenced code block kept whole as one."""
    blocks: list[str] = []
    cur: list[str] = []
    fence = False
    for line in (markdown or "").split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and not line.strip() and cur:
            blocks.append("\n".join(cur))
            cur = []
        elif line.strip() or fence or cur:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def _hard_split(block: str, size: int) -> list[str]:
    """A block too long for one message, by lines; a code block is closed and reopened."""
    lines = block.split("\n")
    opener = lines[0].strip() if lines[0].lstrip().startswith("```") else ""
    closer = "\n```" if opener else ""
    room = max(1, size - len(opener) - len(closer) - 1)
    pieces = [line[k:k + room] for line in lines for k in range(0, max(len(line), 1), room)]
    out: list[str] = []
    cur: list[str] = []
    length = 0
    for piece in pieces:
        if cur and length + len(piece) + 1 > room:
            out.append("\n".join(cur) + closer)
            cur, length = ([opener], len(opener)) if opener else ([], 0)
        cur.append(piece)
        length += len(piece) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def chunks(markdown: str, size: int = CHUNK) -> list[str]:
    """Messages of at most `size`, split between paragraphs and never inside a code block."""
    out: list[str] = []
    cur = ""
    for block in _blocks(markdown):
        if len(block) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.extend(_hard_split(block, size))
        elif cur and len(cur) + 2 + len(block) > size:
            out.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        out.append(cur)
    return out


# Files an answer names under the outputs directory — the same shapes the
# console's outputs.ts finds: a repo path, outputs/…, or the /outputs/ URL.
_OUTPUT_PATH = re.compile(
    r"(?:knowledge_base/)?outputs/([^\s`\"'()<>\[\]，。、；：）]+?\.(?:md|txt|json|html|csv|svg|drawio|pdf|png|jpe?g|gif|webp|m4a))(?![\w.])",
    re.I)
MAX_FILES = 10


def output_files(*texts: str) -> list[Path]:
    """The existing files under the outputs folder that `texts` name, in order, once each."""
    from meeseek.config.paths import home

    root = home().outputs.resolve()
    found: list[Path] = []
    for text in texts:
        for m in _OUTPUT_PATH.finditer(text or ""):
            path = (root / m[1]).resolve()
            try:
                path.relative_to(root)   # nothing outside the outputs directory
            except ValueError:
                continue
            if path.is_file() and path not in found:
                found.append(path)
    return found[:MAX_FILES]


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def detach(markdown: str, files: list[Path], *, caption: bool = False) -> str:
    """The answer without the paths of files that go with it as attachments:
    each becomes a pointer to the attachment — or, under a photo (`caption`),
    nothing, along with a label left bare on its line ("圖片：")."""
    from meeseek.config.paths import home

    root = home().outputs.resolve()
    attached = set(files)

    def swap(m: re.Match) -> str:
        path = (root / m[1]).resolve()
        if path not in attached:
            return m[0]
        if caption:
            return ""
        return "（見下方圖片）" if path.suffix.lower() in IMAGE_SUFFIXES else f"（附檔：{path.name}）"

    # An embedded image (![alt](/outputs/…)) goes whole; a bare path is swapped.
    text = re.sub(r"!\[[^\]]*\]\(([^)\s]+)\)",
                  lambda m: _OUTPUT_PATH.sub(swap, m[1].lstrip("/")) if _OUTPUT_PATH.fullmatch(m[1].lstrip("/")) else m[0],
                  markdown or "")
    text = _OUTPUT_PATH.sub(swap, text)
    if caption:
        text = "\n".join(line for line in text.split("\n")
                         if not re.fullmatch(r"\s*[^\s：:]{0,10}[：:]\s*", line))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def _turn_texts(item: dict) -> list[str]:
    """What the item's last turn said and its tools returned — where a file it made is named."""
    from meeseek.runtime import history

    try:
        found = history.rows(session=item["session_id"], users=(item["user_id"],))
    except Exception:  # noqa: BLE001 — the reply alone still names most files
        logger.debug("session read failed", exc_info=True)
        return []
    start = max((n for n, r in enumerate(found) if r.event.get("author") == "user"), default=-1)
    texts: list[str] = []
    for r in found[start + 1:]:
        for part in (r.event.get("content") or {}).get("parts") or []:
            if part.get("function_response") is not None:
                texts.append(json.dumps(part["function_response"].get("response"), ensure_ascii=False, default=str))
            elif part.get("text"):
                texts.append(part["text"])
    return texts


def describe(action: dict) -> str:
    """A held write, as the question the chat asks."""
    if action["tool"] == "claude_fix":     # a daily review's fix (runtime/daily_review.py)
        a = action["args"]
        return (f"要讓 Claude 在新分支 {a['branch']} 修這項嗎？（commit，不 push）\n\n"
                f"{a['title']}\n{a['fix'][:_ARGS_CHARS]}")
    args = json.dumps(action["args"], ensure_ascii=False, indent=1)
    if len(args) > _ARGS_CHARS:
        args = args[:_ARGS_CHARS] + "\n…"
    return f"要執行 {action['tool']} 嗎？\n\n{args}"


# --- conversations ---------------------------------------------------------------

def conversation(channel: str, chat: str, now: float | None = None) -> dict | None:
    """The chat's conversation still going, if any."""
    now = now or time.time()
    mine = [i for i in inbox.items(1000) if i["source"] == channel and i["meta"].get("chat") == chat]
    if not mine:
        return None
    latest = max(mine, key=lambda i: i["created_at"])
    if latest["meta"].get("closed") or latest["status"] == "dismissed" or now - latest["updated_at"] > IDLE_S:
        return None
    return latest


def _start(ch: Channel, chat: str, item_id: str, message: str) -> None:
    done = app().answered[item_id] = asyncio.Event()
    inbox.start(item_id, message)
    asyncio.get_running_loop().create_task(_keep_typing(ch, chat, done))


async def _keep_typing(ch: Channel, chat: str, done: asyncio.Event, limit_s: float = 1800) -> None:
    """The chat's typing indicator, kept on until the answer is sent."""
    end = time.monotonic() + limit_s
    while not done.is_set() and time.monotonic() < end:
        try:
            await ch.typing(chat)
        except Exception:  # noqa: BLE001 — cosmetic
            logger.debug("typing indicator failed", exc_info=True)
        try:
            await asyncio.wait_for(done.wait(), timeout=ch.typing_every)
        except asyncio.TimeoutError:
            pass


async def on_text(ch: Channel, chat: str, user: str, text: str) -> None:
    """A message from the chat."""
    if not ch.allowed(user):
        if not ch.configured():
            await ch.send(chat, [f"你的 {ch.label} user id 是：\n{user}\n\n"
                                 f"把它加進 project.yaml 的 messengers.{ch.name}_allowed_users，重啟 server 後就能用。"])
        else:
            logger.info("[%s] ignored a message from %s", ch.name, user)
        return
    text = (text or "").strip()
    if not text:
        return
    item = conversation(ch.name, chat)
    if text in ("/new", "/reset"):
        _claude_targets.pop(f"{ch.name}:{chat}", None)
        if item:
            item["meta"]["closed"] = True
            if item["status"] in ("ready", "needs_input"):
                item["status"] = "done"
            inbox.save(item)
        await ch.send(chat, ["好，下一則訊息開始新的對話。"])
        return
    if text == "/claude" or text.startswith("/claude "):
        text = await _claude_command(ch, chat, text.removeprefix("/claude").strip())
        if not text:
            return
    if text in ("/start", "/help"):
        await ch.send(chat, [HELP])
        return
    title = text.splitlines()[0][:80]
    if target := _claude_targets.get(f"{ch.name}:{chat}"):
        from meeseek.runtime import claude_agent

        text = f"{claude_agent.directive(target['session_id'], target.get('title', ''))}\n{text}"
    if item and (item["id"] in app().answered or item["status"] == "working"):   # a turn is still running
        item["meta"].setdefault("queued", []).append(text)
        inbox.save(item)
        await ch.send(chat, ["收到，做完上一則就接著處理這則。"])
        return
    if item is None:
        item = inbox.file(source=ch.name, key=f"{ch.name}:{chat}:{uuid.uuid4().hex[:8]}",
                          title=title, sender="You", body=text,
                          meta={"chat": chat})
        item["triage"] = {"action": "investigate", "why": f"From your phone ({ch.label})"}
        inbox.save(item)
        message = prompts.text("runtime/trigger#phone", app=ch.label, text=text)
    else:
        message = text
    _start(ch, chat, item["id"], message)


_CHOICE = re.compile(r"([ar]):([a-f0-9]+):([a-f0-9]+)")
_RATING = re.compile(r"f:([+-]):([a-f0-9]+)")
_PROPOSAL = re.compile(r"p:([ar]):([a-f0-9]+)")
_DIGEST = re.compile(r"d:s:(\d+)")   # 全部標為已讀 under an Inbox digest, stamped when it was sent
ASK_WHY = "ask_why"                       # on_choice's answer to a 👎: ask what was wrong
_rated: dict[str, tuple[str, str]] = {}   # chat → (invocation, session) of the last 👎, awaiting its note


async def on_choice(ch: Channel, chat: str, user: str, data: str) -> str:
    """An Approve / Reject tapped in the chat; returns what to say back."""
    if not ch.allowed(user):
        return ""
    if r := _RATING.fullmatch(data or ""):
        return _rate(ch, chat, r[2], 1 if r[1] == "+" else -1)
    if p := _PROPOSAL.fullmatch(data or ""):
        return _decide_proposal(p[2], p[1] == "a")
    if d := _DIGEST.fullmatch(data or ""):
        return _digest_seen(chat, float(d[1]))
    m = _CHOICE.fullmatch(data or "")
    item = inbox.get(m[2]) if m else None
    review = item is not None and inbox.kind_of(item) == "review"
    if review:
        from meeseek.config import cfg

        if chat != cfg.triggers.coach_telegram_chat:
            return "找不到這個動作。"
    elif item is None or item["source"] != ch.name or item["meta"].get("chat") != chat:
        return "找不到這個動作。"
    try:
        if m[1] == "a":
            message = inbox.approve(item["id"], m[3])
        else:
            message = inbox.reject(item["id"], m[3], "使用者在手機上拒絕")
    except ValueError:
        return "這個動作已經處理過了。"
    if review:    # the fix runs as a Claude session; its result comes back here when it ends
        inbox.start(item["id"], message)
        return "已同意，Claude 開新分支動手修…" if m[1] == "a" else "已拒絕這項。"
    _start(ch, chat, item["id"], message)
    return "已同意，繼續執行…" if m[1] == "a" else "已拒絕，我換個做法。"


def _rate(ch: Channel, chat: str, item_id: str, value: int) -> str:
    """A 👍/👎 tapped under a reply: recorded against the item's last turn."""
    from meeseek.runtime import feedback

    item = inbox.get(item_id)
    if item is None or item["meta"].get("chat") != chat:
        return "找不到這則回覆。"
    inv = inbox.last_invocation(item.get("session_id", ""))
    feedback.record(inv, item.get("session_id", ""), "thumb", value=value, channel=ch.name)
    if value < 0:
        _rated[chat] = (inv, item.get("session_id", ""))
        return ASK_WHY
    return "收到 👍"


def _decide_proposal(pid: str, approve: bool) -> str:
    """Approve or reject an improvement proposal from the phone (runtime/improve.py)."""
    from meeseek.runtime import improve

    try:
        out = improve.decide(pid, approve, note="" if approve else "在手機上拒絕", by="phone")
    except ValueError:
        return "這個提案已經處理過了。"
    if out["status"] == "approved":
        return "已同意並套用" + (f"（commit {out['commit']}）" if out.get("commit") else "") + "，下一輪就生效。"
    if out["status"] == "rejected":
        return "已拒絕。想說原因，到 Learning 頁留一句，下次會參考。"
    return f"沒有套用：{out.get('why', out['status'])}"


# --- quiet hours -------------------------------------------------------------------

_QUIET = re.compile(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*")


def quiet_until(now: float | None = None) -> float:
    """When `triggers.quiet_hours` (local hours, "23-8") holds the bot's own
    pushes: the time they end, or 0.0 — send now. Empty or unreadable = no
    quiet hours."""
    from meeseek.config import cfg

    m = _QUIET.fullmatch(cfg.triggers.quiet_hours or "")
    if m is None:
        return 0.0
    start, end = int(m[1]) % 24, int(m[2]) % 24
    if start == end:
        return 0.0
    dt = datetime.fromtimestamp(now if now is not None else time.time()).astimezone()
    in_quiet = (dt.hour >= start or dt.hour < end) if start > end else (start <= dt.hour < end)
    if not in_quiet:
        return 0.0
    end_dt = dt.replace(hour=end, minute=0, second=0, microsecond=0)
    if end_dt <= dt:
        end_dt += timedelta(days=1)
    return end_dt.timestamp()


def _hold(until: float, *, push: str, **ids) -> None:
    """A push the quiet hours stop becomes a wake due when they end
    (runtime/wakes.py `quiet_push`): on disk, so a restart does not lose it,
    and keyed, so holding the same push twice files one wake."""
    from meeseek.runtime import wakes

    ref = next(iter(ids.values()))
    wakes.file(kind="quiet_push", due_at=until, why=f"安靜時段，{push} 推播延後",
               payload={"push": push, **ids}, key=f"quiet:{push}:{ref}")


async def announce_proposals() -> int:
    """Tell your phone about each new improvement proposal, once, with Approve /
    Reject; the Learning page has the full diff. How many were sent."""
    from meeseek.config import cfg
    from meeseek.runtime import improve

    ch, chat = channels().get("telegram"), cfg.triggers.coach_telegram_chat
    if ch is None or not chat or not hasattr(ch, "call"):
        return 0
    if quiet_until():
        return 0   # the 15-minute cron after the hours sends them; unannounced() keeps them
    n = 0
    for p in improve.unannounced():
        better = list(p.get("outcomes", {}).values()).count("cand")
        text = (f"meeseek 想改進「{p['skill']}」\n{p['summary']}\n\n"
                + (f"重播 {len(p.get('outcomes', {}))} 個案例，{better} 個變好、沒有變差；花費 US${p.get('cost_usd', 0)}\n"
                   if p["kind"] == "edit" else "")
                + (f"經驗：{p['lesson']['lesson']}\n" if p.get("lesson") else "")
                + "完整修改在 console 的 Learning 頁。")
        await ch.call("sendMessage", chat_id=chat, text=text[:4000],
                      reply_markup={"inline_keyboard": [[{"text": "同意套用", "callback_data": f"p:a:{p['id']}"},
                                                         {"text": "拒絕", "callback_data": f"p:r:{p['id']}"}]]})
        improve.announced(p["id"])
        n += 1
    return n


# --- the Inbox digest ---------------------------------------------------------------


# what the last digest showed, so a repeat says nothing new
def digest_state_file() -> Path:
    return home().data / "inbox_digest.json"


_DIGEST_LIST = 5
_SOURCE_NAMES = {"gmail": "信", "chat": "Chat", "meeting": "會議", "calendar": "日曆", "alert": "警報",
                 "telegram": "手機"}


def digest_items() -> list[dict]:
    """What waits for the person and was never seen: finished answers, held
    writes, open questions — fyi and settled items are not a to-do."""
    want = ("ready", "needs_approval", "needs_input")
    return [i for i in inbox.items(limit=500) if i["status"] in want and not i.get("seen")]


def _digest_text(items: list[dict], now: float | None = None, stale: list[dict] = ()) -> str:
    now = now or time.time()
    if not items:   # only stale approvals to name
        return _stale_text(stale) + "\n\n到 console 的 Inbox 頁處理。"
    approvals = sum(1 for i in items if i["status"] == "needs_approval")
    questions = sum(1 for i in items if i["status"] == "needs_input")
    srcs = {}
    for i in items:
        name = _SOURCE_NAMES.get(i["source"], i["source"])
        srcs[name] = srcs.get(name, 0) + 1
    head = f"Inbox 有 {len(items)} 件處理好了沒人看（" + "、".join(f"{k} {v}" for k, v in srcs.items()) + "）"
    if approvals:
        head += f"，其中 {approvals} 件有動作等你批准"
    if questions:
        head += f"，{questions} 件在等你回答"
    ago = lambda t: f"{int((now - t) // 86400)} 天前" if now - t >= 86400 else f"{int((now - t) // 3600)} 小時前"  # noqa: E731
    oldest = sorted(items, key=lambda i: i.get("received_at") or i["created_at"])
    lines = [f"· {'⏸ ' if i['status'] == 'needs_approval' else '❓ ' if i['status'] == 'needs_input' else ''}"
             f"{i['title'][:50]}（{ago(i.get('received_at') or i['created_at'])}）" for i in oldest[:_DIGEST_LIST]]
    more = f"\n…還有 {len(items) - _DIGEST_LIST} 件" if len(items) > _DIGEST_LIST else ""
    tail = "\n\n" + _stale_text(stale) if stale else ""
    return head + "。最久的：\n" + "\n".join(lines) + more + tail + "\n\n到 console 的 Inbox 頁處理。"


def _stale_text(stale: list[dict]) -> str:
    """Held writes waiting past inbox.STALE_APPROVAL_S: named once, then they expire."""
    days = inbox.STALE_APPROVAL_S // 86400
    lines = [f"· ⏸ {i['title'][:50]}" for i in stale[:_DIGEST_LIST]]
    more = f"\n…還有 {len(stale) - _DIGEST_LIST} 件" if len(stale) > _DIGEST_LIST else ""
    return (f"這 {len(stale)} 件的動作等你批准超過 {days} 天，明天就過期（不會執行）：\n"
            + "\n".join(lines) + more)


async def inbox_digest() -> int:
    """The waiting items, summed up on the phone — on the digest cron, so what
    meeseek finishes stops sinking unseen (81 items had, 2026-10-23). Sent to
    the personal chat, held by the quiet hours, and silent when nothing changed
    since the last digest. 1 when one went out."""
    from meeseek.config import cfg

    ch, chat = channels().get("telegram"), cfg.triggers.coach_telegram_chat
    if ch is None or not chat or not hasattr(ch, "call"):
        return 0
    items, stale = digest_items(), inbox.stale_approvals()
    if not items and not stale:
        return 0
    stamp = hashlib.sha1(",".join(sorted(i["id"] for i in items + stale)).encode()).hexdigest()
    try:
        if json.loads(digest_state_file().read_text())["sent"] == stamp:
            return 0
    except (OSError, ValueError, KeyError):
        pass
    if until := quiet_until():
        _hold(until, push="digest", ref="digest")
        return 0
    now = time.time()
    await ch.call("sendMessage", chat_id=chat, text=_digest_text(items, now, stale)[:4000],
                  reply_markup={"inline_keyboard": [[{"text": "全部標為已讀", "callback_data": f"d:s:{int(now)}"}]]})
    digest_state_file().write_text(json.dumps({"sent": stamp, "at": now}))
    inbox.note_stale([i["id"] for i in stale], now)
    return 1


def _digest_seen(chat: str, until_ts: float) -> str:
    """全部標為已讀 under a digest: every unseen item the digest could have
    shown is marked seen; held approvals stay pending on the Inbox page."""
    from meeseek.config import cfg

    if chat != cfg.triggers.coach_telegram_chat:
        return "找不到這份摘要。"
    n = 0
    for i in digest_items():
        if i.get("updated_at", 0) <= until_ts:
            inbox.mark_seen(i["id"])
            n += 1
    approvals = sum(1 for i in digest_items() if i["status"] == "needs_approval")
    tail = f"；{approvals} 件待批准的動作仍在 Inbox 頁等你" if approvals else ""
    return f"已標為已讀（{n} 件）{tail}。"


async def on_note(ch: Channel, chat: str, user: str, text: str) -> None:
    """Your answer to "what was wrong?": the 👎 again, with its note (the latest counts)."""
    from meeseek.runtime import feedback

    if not ch.allowed(user) or chat not in _rated:
        return
    inv, session = _rated.pop(chat)
    feedback.record(inv, session, "thumb", value=-1, note=(text or "").strip()[:1000], channel=ch.name)
    await ch.send(chat, ["記下了，謝謝。之後會照這個改進。"])


async def deliver(item: dict) -> None:
    """After a turn on a phone's item: the answer, then a question per new held write."""
    ch = channels().get(item["source"]) if item else None
    if ch is None:
        return
    chat = item["meta"]["chat"]
    if (ev := app().answered.pop(item["id"], None)) is not None:
        ev.set()
    sent = item["meta"].setdefault("sent_files", [])
    files = [p for p in output_files(item["reply"], *await _turn_texts(item)) if str(p) not in sent]
    if item["status"] == "failed":
        await ch.send(chat, [f"這一則沒有完成：{item['error']}\n再傳一次訊息，或到 Inbox 頁重試。"])
        files = []
    else:
        done = await ch.send_reply(chat, item["reply"] or "（沒有回覆內容）", files)
        if (rate := getattr(ch, "rate", None)) is not None:
            await rate(chat, item["id"])     # 👍/👎 under the answer, before files and questions follow
        sent.extend(str(p) for p in done)
        files = [p for p in files if p not in done]
    item = inbox.get(item["id"]) or item
    item["meta"]["sent_files"] = sent
    if files:
        try:
            await ch.send_files(chat, files)
        except Exception as e:  # noqa: BLE001 — the answer is out; say what did not follow it
            logger.exception("[%s] sending files failed", ch.name)
            await ch.send(chat, [f"檔案沒傳成功（{e}），可以在 console 開：\n" +
                                 "\n".join(f"outputs/{p.name}" for p in files)])
        sent.extend(str(p) for p in files)
    for action in item["actions"]:
        if action["status"] == "pending" and not action.get("asked"):
            await ch.ask(chat, item, action)
            action["asked"] = True
    if item["status"] == "ready":
        item["status"] = "done"   # answered, on your phone
    queued = item["meta"].pop("queued", [])
    inbox.save(item)
    if queued:
        _start(ch, chat, item["id"], "\n\n".join(queued))


# --- Claude Code from the phone ----------------------------------------------------

_claude_lists: dict[str, list[tuple[str, str]]] = {}   # chat → (session id, title) its last /claude listed
_claude_targets: dict[str, dict] = {}   # "<channel>:<chat>" → the Claude session its tasks go to


async def _claude_command(ch: Channel, chat: str, rest: str) -> str:
    """Pick the Claude session this chat's tasks go to; meeseek still does the
    handing over. `/claude` lists; `/claude <n>` or `/claude new` sets the
    target (with a message after it, that message goes too); `/claude off`
    clears it. Returns the message to carry on with, if any."""
    from meeseek.runtime import claude_agent

    key = f"{ch.name}:{chat}"
    m = re.fullmatch(r"(\d+|new|off)(?:\s+(.+))?", rest, re.S)
    if not m:
        rows = await asyncio.to_thread(claude_agent.sessions, "", 10)
        _claude_lists[chat] = [(r["session_id"], r["title"]) for r in rows]
        if not rows:
            await ch.send(chat, ["這台電腦上還沒有 Claude Code 的 session。/claude new 讓之後的任務開一個新的。"])
            return ""
        lines = [f"{n}. {r['title']}{'（Desktop）' if r.get('desktop') else ''}\n   {r['cwd'].replace(str(Path.home()), '~')}"
                 f"{' · ' + r['status'] if r['status'] else ''}" for n, r in enumerate(rows, 1)]
        now = _claude_targets.get(key)
        await ch.send(chat, ["Claude Code session（新到舊）：\n\n" + "\n".join(lines) +
                             "\n\n/claude <編號> 指定之後的任務交給它；/claude new 開新的；/claude off 取消。" +
                             (f"\n目前指定：{now['title']}" if now else "")])
        return ""
    if m[1] == "off":
        _claude_targets.pop(key, None)
        await ch.send(chat, ["好，之後的任務不指定 Claude session，由我判斷。"])
        return ""
    if m[1] == "new":
        _claude_targets[key] = {"session_id": "new", "title": "新的 Claude session"}
    else:
        listed = _claude_lists.get(chat, [])
        n = int(m[1])
        if not 1 <= n <= len(listed):
            await ch.send(chat, ["先傳 /claude 看清單，再用清單上的編號。"])
            return ""
        sid, title = listed[n - 1]
        _claude_targets[key] = {"session_id": sid, "title": title}
    target = _claude_targets[key]
    if not m[2]:
        await ch.send(chat, [f"好，之後的任務交給 Claude：{target['title']}。/claude off 取消。"])
    return (m[2] or "").strip()


def _claude_chat(record: dict) -> tuple[Channel | None, str]:
    """Where a Claude record's result is owed: the chat that asked for it,
    directly or through a phone conversation with meeseek."""
    origin = record.get("origin") or {}
    if origin.get("channel") in channels():
        return channels()[origin["channel"]], origin.get("chat", "")
    item = inbox.by_session(origin.get("session_id", "")) if origin.get("session_id") else None
    if item and item["source"] in channels():
        return channels()[item["source"]], item["meta"].get("chat", "")
    return None, ""


async def claude_done(record: dict) -> None:
    """A Claude turn ended: its answer, to the chat that asked — unless the
    agent that asked is still waiting on it and will relay it itself, or a
    follow-up wake (runtime/wakes.py) will take the result over with a proper
    turn instead of this raw push."""
    from meeseek.runtime import claude_agent, wakes

    if time.time() < record.get("await_until", 0):
        return
    if wakes.exists(f"claude:{record['id']}"):
        return
    ch, chat = _claude_chat(record)
    if ch is None or not chat:
        return
    if until := quiet_until():
        _hold(until, push="claude_done", record_id=record["id"])
        return
    reply = claude_agent.last_reply(record)
    head = f"**Claude** · {record['title']}"
    if record["status"] == "failed":
        head += "（沒有完成）"
    await ch.send_reply(chat, f"{head}\n\n{reply}", [])


async def remind_push(session_id: str, text: str) -> bool:
    """A remind wake came due (runtime/wakes.py): the text, as a plain message
    to the chat whose conversation asked for it — or, when the asking session
    was not a chat's (the console), to the personal chat
    (`triggers.coach_telegram_chat`). True when it went out; the wake itself
    holds it through the quiet hours, not this."""
    from meeseek.config import cfg

    item = inbox.by_session(session_id) if session_id else None
    if item is not None and item["source"] in channels():
        ch, chat = channels()[item["source"]], item["meta"].get("chat", "")
    else:
        ch, chat = channels().get("telegram"), cfg.triggers.coach_telegram_chat
    if ch is None or not chat:
        return False
    await ch.send(chat, [f"⏰ {text}"])
    return True


# --- the meeting coach, on your phone ---------------------------------------------

async def coach_push(item: dict) -> None:
    """A coached meeting's rehearsal, or its report with the report, transcript
    and recording, to the one chat `triggers.coach_telegram_chat` names — once."""
    from meeseek.config import cfg
    from meeseek.runtime.meeting_coach import coached

    chat, ch = cfg.triggers.coach_telegram_chat, channels().get("telegram")
    if not item or not chat or ch is None or item["meta"].get("pushed") or not item.get("reply"):
        return
    if item["status"] not in ("ready", "needs_approval", "needs_input", "done"):
        return
    title = item["title"].removeprefix("Recording: ")
    if item["source"] == "calendar" and coached(title):
        head, files = f"**會前推演** · {title}", []
    elif item["source"] == "meeting" and item["meta"].get("coach"):
        head = f"**會後報告** · {title}"
        files = [home().root / p for p in item["meta"]["coach"].values() if (home().root / p).is_file()]
    else:
        return
    if until := quiet_until():
        _hold(until, push="coach", item_id=item["id"])   # `pushed` stays unclaimed until it goes out
        return
    item = inbox.get(item["id"]) or item
    item["meta"]["pushed"] = True       # claimed before sending: a slow send is not sent twice
    inbox.save(item)
    sent = await ch.send_reply(chat, f"{head}\n\n{item['reply']}", files)
    rest = [p for p in files if p not in sent]
    if rest:
        await ch.send_files(chat, rest)


async def meeting_failed(item: dict) -> None:
    """A coached meeting that gave no report says so in the coach's chat, with
    why — once. Silence read as "nothing to report" (2026-10-21: a daily
    never recorded, and nobody heard)."""
    from meeseek.config import cfg
    from meeseek.runtime.meeting_coach import coached

    chat, ch = cfg.triggers.coach_telegram_chat, channels().get("telegram")
    title = item["title"].removeprefix("Recording: ")
    if not chat or ch is None or item["meta"].get("pushed") or not coached(title):
        return
    if until := quiet_until():
        _hold(until, push="meeting_failed", item_id=item["id"])
        return
    item = inbox.get(item["id"]) or item
    item["meta"]["pushed"] = True
    inbox.save(item)
    try:
        await ch.send(chat, [f"會後報告沒有產生 · {title}\n{item.get('error') or '原因不明，見 Inbox'}"])
    except Exception:  # noqa: BLE001 — the item says it on the Inbox page all the same
        logger.warning("could not tell the coach chat about %s", item["id"], exc_info=True)


# --- lifecycle -------------------------------------------------------------------

_stops: list = []


async def start() -> None:
    """Bring up the messengers project.yaml turns on. A failure is a source
    error on the Inbox page, never a failed server start."""
    from meeseek.config import cfg

    if cfg.messengers.telegram:
        from meeseek.runtime.messengers import telegram

        try:
            _stops.append(await telegram.start())
        except Exception as e:  # noqa: BLE001
            logger.exception("[telegram] could not start")
            set_status("telegram", False, f"{type(e).__name__}: {e}")


async def stop() -> None:
    while _stops:
        try:
            await _stops.pop()()
        except Exception:  # noqa: BLE001
            logger.debug("messenger stop failed", exc_info=True)


def enabled() -> list[str]:
    from meeseek.config import cfg

    return [n for n, on in (("telegram", cfg.messengers.telegram),) if on]
