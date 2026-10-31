"""Telegram, through a bot from @BotFather.

We call Telegram: a long poll (getUpdates) waits for messages, so nothing is
exposed and no URL is needed. The offset of the last update handled is kept
in state_file(), so a restart does not answer a message twice. Held writes
arrive with an inline Approve / Reject keyboard; once tapped, the keyboard is
taken off the message.

An answer is Markdown; Telegram renders a small HTML subset, so to_html()
translates it — bold, italic, code, code blocks, links, quotes; lists become
bullets and tables monospace columns. A message Telegram cannot parse is sent
again as plain text. Files the turn made go after it: images as photos (an
album when there are several), the rest as documents.

The laptop may be on a flaky hotspot when a turn ends, so a send rides out a
dropped network, a 5xx or a 429 for a few minutes before giving up; a refusal
(a 4xx such as HTML Telegram cannot parse) fails at once, as it is not going
to change. The poll keeps its own loop instead, and is never retried here.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import mimetypes
import os
import re
import time
import unicodedata
from pathlib import Path

import httpx

from meeseek.config.paths import home
from meeseek.runtime import messengers, page_shot

logger = logging.getLogger(__name__)


def state_file() -> Path:
    return home().data / "telegram_state.json"


_POLL_S = 25
_POLL_BACKOFF_MAX = 30   # a poll failing on and off: back within half a minute of the network
_RETRY_S = 300           # how long a send keeps trying through a dropped network
_PHOTO = {".png", ".jpg", ".jpeg", ".webp"}
_PHOTO_MAX = 10 * 1024 * 1024     # Telegram's limits for an upload
_FILE_MAX = 50 * 1024 * 1024
_ALBUM = 10
_CAPTION = 1000   # Telegram allows 1024 characters under a photo


# --- Markdown → Telegram HTML -------------------------------------------------

def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _inline(text: str) -> str:
    out = []
    for part in re.split(r"(`[^`\n]+`)", text):
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{_esc(part[1:-1])}</code>")
            continue
        part = _esc(part)
        part = re.sub(r"!\[([^\]]*)\]\([^)\s]+\)", "", part)   # an image is sent as a file
        part = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                      lambda m: f'<a href="{m[2]}">{m[1]}</a>' if m[2].startswith(("http://", "https://")) else m[1],
                      part)
        part = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<b>{m[1] or m[2]}</b>", part)
        part = re.sub(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])", r"<i>\1</i>", part)
        part = re.sub(r"~~(.+?)~~", r"<s>\1</s>", part)
        out.append(part)
    return "".join(out)


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _table(rows: list[str]) -> str:
    """A Markdown table as monospace columns — Telegram has no tables."""
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows
             if not re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", r)]
    n = max(len(r) for r in cells)
    cells = [r + [""] * (n - len(r)) for r in cells]
    widths = [max(_width(r[i]) for r in cells) for i in range(n)]
    lines = ["  ".join(c + " " * (w - _width(c)) for c, w in zip(r, widths)).rstrip() for r in cells]
    return f"<pre>{_esc(chr(10).join(lines))}</pre>"


def to_html(markdown: str) -> str:
    lines = (markdown or "").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if m := re.match(r"\s*```\s*([\w+#-]*)", line):
            body = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            code = _esc("\n".join(body))
            out.append(f'<pre><code class="language-{m[1]}">{code}</code></pre>' if m[1] else f"<pre>{code}</pre>")
            continue
        if line.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue
        if line.lstrip().startswith(">"):
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(_inline(lines[i].lstrip()[1:].lstrip()))
                i += 1
            out.append(f"<blockquote>{chr(10).join(quote)}</blockquote>")
            continue
        if m := re.match(r"#{1,6}\s+(.*)", line):
            out.append(f"<b>{_inline(m[1])}</b>")
        elif m := re.match(r"(\s*)[-*+]\s+(.*)", line):
            out.append("  " * (len(m[1]) // 2) + "• " + _inline(m[2]))
        elif re.fullmatch(r"\s*([-*_])\s*(\1\s*){2,}", line):
            out.append("──────")
        else:
            out.append(_inline(line))
        i += 1
    return "\n".join(out).strip()


class Telegram:
    name = "telegram"
    label = "Telegram"
    typing_every = 4.5   # "typing…" lasts five seconds

    def __init__(self, token: str, allowed_users: list[str]):
        self._api = f"https://api.telegram.org/bot{token}"
        self._allowed = {str(u) for u in allowed_users}
        self._last: dict[str, int] = {}       # chat → the last message sent there (a reply's last chunk)
        self._asking: dict[str, int] = {}     # chat → the "what was wrong?" prompt waiting for its answer

    def allowed(self, user: str) -> bool:
        return user in self._allowed

    def configured(self) -> bool:
        return bool(self._allowed)

    async def call(self, method: str, *, http_timeout: float = 15, retry: bool = True, **params) -> dict:
        return await self._post(method, http_timeout, retry, json=params)

    async def upload(self, method: str, data: dict, files: dict) -> dict:
        return await self._post(method, 120, True, data=data, files=files)

    async def _post(self, method: str, http_timeout: float, retry: bool, **request) -> dict:
        """One API call. With `retry`, a failure that may pass (no network, a
        5xx, a 429) is tried again for up to _RETRY_S; any other raises at once.
        A fresh client each time: after a network switch a pooled connection
        is a dead one."""
        deadline = time.monotonic() + (_RETRY_S if retry else 0)
        delay = 2.0
        while True:
            try:
                async with httpx.AsyncClient(timeout=http_timeout) as client:
                    r = await client.post(f"{self._api}/{method}", **request)
            except httpx.TransportError as e:
                failure: Exception = e
                wait = delay
            else:
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                if body.get("ok"):
                    res = body.get("result")
                    chat = (request.get("json") or request.get("data") or {}).get("chat_id")
                    if method.startswith("send") and chat and isinstance(res, dict) and res.get("message_id"):
                        self._last[str(chat)] = res["message_id"]
                    return res
                failure = RuntimeError(f"Telegram {method}: {body.get('description') or r.status_code}")
                if r.status_code != 429 and r.status_code < 500:
                    raise failure
                wait = float((body.get("parameters") or {}).get("retry_after") or delay)
            if time.monotonic() + wait > deadline:
                raise failure
            logger.info("[telegram] %s failed (%s); trying again in %.0fs", method, failure, wait)
            await asyncio.sleep(wait)
            delay = min(delay * 2, 30)

    async def send(self, chat: str, texts: list[str]) -> None:
        for text in texts:
            if text:
                await self.call("sendMessage", chat_id=chat, text=text[:4096])

    async def send_reply(self, chat: str, markdown: str, files: list[Path]) -> list[Path]:
        photos = [p for p in files if p.suffix.lower() in _PHOTO and p.stat().st_size <= _PHOTO_MAX]
        if len(files) == 1 and photos:
            # One picture and a short answer: one message, the answer under the photo.
            caption = messengers.detach(markdown, files, caption=True)
            if len(messengers.plain(caption)) <= _CAPTION:
                photo = {"photo": (photos[0].name, photos[0].read_bytes(), mimetypes.guess_type(photos[0].name)[0])}
                try:
                    await self.upload("sendPhoto", {"chat_id": chat, "caption": to_html(caption),
                                                    "parse_mode": "HTML"}, photo)
                except RuntimeError:
                    await self.upload("sendPhoto", {"chat_id": chat, "caption": messengers.plain(caption)}, photo)
                return photos
        await self._send_text(chat, messengers.detach(markdown, files))
        return []

    async def _send_text(self, chat: str, markdown: str) -> None:
        for part in messengers.chunks(markdown):
            try:
                await self.call("sendMessage", chat_id=chat, text=to_html(part)[:4096], parse_mode="HTML",
                                link_preview_options={"is_disabled": True})
            except RuntimeError as e:   # HTML Telegram will not parse: the same, as plain text
                logger.info("[telegram] HTML refused (%s); sending plain", e)
                await self.send(chat, [messengers.plain(part)])

    async def send_files(self, chat: str, paths: list[Path]) -> None:
        # A page cannot be opened in the chat: it goes as a picture of itself,
        # at phone size, besides the file.
        for page in [p for p in paths if p.suffix.lower() in (".html", ".htm")]:
            shot = await asyncio.to_thread(page_shot.screenshot, page)
            if shot is not None:
                await self.upload("sendPhoto", {"chat_id": chat, "caption": page.name},
                                  {"photo": (f"{page.stem}.png", shot.read_bytes(), "image/png")})
        photos = [p for p in paths if p.suffix.lower() in _PHOTO and p.stat().st_size <= _PHOTO_MAX]
        too_big = [p for p in paths if p.stat().st_size > _FILE_MAX]
        docs = [p for p in paths if p not in photos and p not in too_big]
        part = lambda p: (p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream")  # noqa: E731
        for i in range(0, len(photos), _ALBUM):
            group = photos[i:i + _ALBUM]
            if len(group) == 1:
                await self.upload("sendPhoto", {"chat_id": chat, "caption": group[0].name}, {"photo": part(group[0])})
            else:
                media = [{"type": "photo", "media": f"attach://f{n}", "caption": p.name} for n, p in enumerate(group)]
                await self.upload("sendMediaGroup", {"chat_id": chat, "media": json.dumps(media)},
                                  {f"f{n}": part(p) for n, p in enumerate(group)})
        for p in docs:
            await self.upload("sendDocument", {"chat_id": chat}, {"document": part(p)})
        if too_big:
            await self.send(chat, ["檔案超過 50 MB，在 console 開：\n" + "\n".join(p.name for p in too_big)])

    async def ask(self, chat: str, item: dict, action: dict) -> None:
        ids = f"{item['id']}:{action['id']}"
        await self.call("sendMessage", chat_id=chat, text=messengers.describe(action)[:4096],
                        reply_markup={"inline_keyboard": [[
                            {"text": "同意", "callback_data": f"a:{ids}"},
                            {"text": "拒絕", "callback_data": f"r:{ids}"},
                        ]]})

    async def rate(self, chat: str, item_id: str) -> None:
        """👍/👎 under the reply just sent (runtime/feedback.py)."""
        if not (mid := self._last.get(chat)):
            return
        with contextlib.suppress(RuntimeError):
            await self.call("editMessageReplyMarkup", retry=False, chat_id=chat, message_id=mid,
                            reply_markup={"inline_keyboard": [[{"text": "👍", "callback_data": f"f:+:{item_id}"},
                                                               {"text": "👎", "callback_data": f"f:-:{item_id}"}]]})

    async def ask_why(self, chat: str) -> None:
        """After a 👎: what was wrong — answered by replying to this message."""
        await self.call("sendMessage", chat_id=chat, text="哪裡不對？直接回覆這則（可略過）",
                        reply_markup={"force_reply": True, "input_field_placeholder": "哪裡不對…"})
        if mid := self._last.get(chat):
            self._asking[chat] = mid

    async def typing(self, chat: str) -> None:
        # Cosmetic, and re-sent every few seconds anyway: never waited on.
        await self.call("sendChatAction", retry=False, chat_id=chat, action="typing")

    async def handle(self, update: dict) -> None:
        if msg := update.get("message"):
            if (msg.get("chat") or {}).get("type") != "private":
                return  # one-to-one chats only, not groups
            chat = str(msg["chat"]["id"])
            replied = (msg.get("reply_to_message") or {}).get("message_id")
            if replied and replied == self._asking.get(chat):
                self._asking.pop(chat, None)
                await messengers.on_note(self, chat, str(msg["from"]["id"]), msg.get("text", ""))
                return
            await messengers.on_text(self, chat, str(msg["from"]["id"]), msg.get("text", ""))
        elif cq := update.get("callback_query"):
            chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
            said = await messengers.on_choice(self, chat, str(cq["from"]["id"]), cq.get("data", ""))
            await self.call("answerCallbackQuery", callback_query_id=cq["id"])
            if said == messengers.ASK_WHY:
                await self.ask_why(chat)
            elif said and cq.get("message"):
                if said.startswith("已"):   # settled: the buttons go
                    with contextlib.suppress(RuntimeError):
                        await self.call("editMessageReplyMarkup", chat_id=chat,
                                        message_id=cq["message"]["message_id"], reply_markup={"inline_keyboard": []})
                await self.send(chat, [said])

    async def run(self) -> None:
        """Long-poll for updates until cancelled."""
        offset = _load_offset()
        backoff = 5
        # Inside the loop, not before it: a server started with no network
        # (Wi-Fi not up yet) would otherwise lose its bot until a restart.
        webhook_cleared = False
        while True:
            try:
                if not webhook_cleared:
                    await self.call("deleteWebhook", retry=False)   # getUpdates refuses while a webhook is set
                    webhook_cleared = True
                updates = await self.call("getUpdates", http_timeout=_POLL_S + 10, retry=False, timeout=_POLL_S,
                                          offset=offset, allowed_updates=["message", "callback_query"])
                messengers.set_status(self.name, True, "" if self._allowed else
                                      "No telegram_allowed_users yet — message the bot to get your id")
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a dropped poll is retried
                messengers.set_status(self.name, False, f"{type(e).__name__}: {e}")
                logger.warning("[telegram] poll failed: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _POLL_BACKOFF_MAX)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                _save_offset(offset)
                try:
                    await self.handle(update)
                except Exception:  # noqa: BLE001 — one bad update must not stop the poll
                    logger.exception("[telegram] update failed")


def _load_offset() -> int:
    try:
        return int(json.loads(state_file().read_text())["offset"])
    except (OSError, ValueError, KeyError):
        return 0


def _save_offset(offset: int) -> None:
    state_file().parent.mkdir(parents=True, exist_ok=True)
    state_file().write_text(json.dumps({"offset": offset}))


async def start():
    """The channel and its poll; returns how to stop it."""
    from meeseek.config import cfg

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN must be set in .env")
    channel = Telegram(token, cfg.messengers.telegram_allowed_users)
    messengers.register(channel)
    task = asyncio.get_running_loop().create_task(channel.run())

    async def stop() -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    return stop
