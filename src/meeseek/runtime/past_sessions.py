"""meeseek's own past conversations — found, and read back.

Every turn meeseek runs is in sessions.db (runtime/sessions.py): the console's
chats, a conversation from the phone, an Inbox item's turn, an MCP caller's
request. `search` finds the ones about something ("the review we did for
Ovien") by what the user and meeseek said in them, newest first; `read` hands
one back as its turns, so it can be picked up again.

What the user typed counts most, meeseek's answers next, and what meeseek
wrote to itself least — an Inbox event's mail or meeting notes, the morning
plan's raw data: those are long and name everyone, and outranked the
conversation meant. Each word counts at most a few times, so a long session
does not win by length. Tool results are not searched at all.

A turn an MCP caller started sees that caller's sessions only: the MCP
server keeps each caller's history apart (runtime/mcp_server.py), and asking
meeseek must not be a way around that. Read-only — the live server writes the
same file.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


MAX_DAYS = 180
# How a turn's message starts when meeseek, not the user, wrote it around the
# user's words (runtime/trigger.md): what follows is what was asked.
_WRAPPERS = ("使用者從", "這是自動進來的事件", "- ")
_AUTO = "這是自動進來的事件"            # an Inbox event: nobody typed what follows
_SYSTEM = ("使用者同意執行", "使用者不同意執行", "（系統）")   # meeseek's own notes in the user's turn
_WEIGHT = {"typed": 4, "replies": 1, "auto": 0.5}
_CAP = 3


@dataclass
class _Session:
    user: str
    id: str
    first: float = 0
    last: float = 0
    asked: list[str] = field(default_factory=list)     # every message in the user's turn, unwrapped
    typed: list[str] = field(default_factory=list)     # the ones the user wrote
    auto: list[str] = field(default_factory=list)      # the ones meeseek wrote to itself
    replies: list[str] = field(default_factory=list)   # meeseek's answers
    agents: set[str] = field(default_factory=set)


def _unwrap(text: str) -> str:
    """The user's own words in a message meeseek wrapped (a phone message,
    an Inbox event): the blocks after the wrapper's."""
    blocks = text.split("\n\n")
    while len(blocks) > 1 and blocks[0].lstrip().startswith(_WRAPPERS):
        blocks.pop(0)
    return "\n\n".join(blocks).strip()


def typed(user: str, text: str) -> str | None:
    """What the user wrote in a user-turn message — unwrapped from a phone
    message's framing — or None when meeseek wrote it to itself (an Inbox
    event, the morning plan's data, an approval note). Shared with the
    nightly reading of what holds of you (context/personal.py)."""
    if user == "workflow" or text.lstrip().startswith((_AUTO, *_SYSTEM)):
        return None
    return _unwrap(text) or None


def _sessions(days: float) -> list[_Session]:
    since = time.time() - min(max(days, 1), MAX_DAYS) * 86400
    from meeseek.runtime import history

    found: dict[tuple[str, str], _Session] = {}
    for r in history.rows(since=since):
        user, sid, ts, event = r.user, r.session, r.at, r.event
        if event.get("partial"):
            continue
        s = found.setdefault((user, sid), _Session(user, sid, first=ts))
        s.last = ts
        author = event.get("author") or ""
        text = "".join(p.get("text") or "" for p in (event.get("content") or {}).get("parts") or []
                       if not p.get("thought"))
        if not text.strip():
            continue
        if author == "user":
            words = typed(user, text)
            s.asked.append(words or _unwrap(text))
            (s.typed if words else s.auto).append(words or _unwrap(text))
        else:
            s.replies.append(text)
            s.agents.add(author)
    return [s for s in found.values() if s.asked]


def _visible(s: _Session, origin: dict | None) -> bool:
    asker = (origin or {}).get("user", "")
    return not asker.startswith("mcp") or s.user == asker


def _where(s: _Session) -> tuple[str, str]:
    """(where it happened, the console link that opens it)."""
    from meeseek.runtime import inbox

    if s.user == "inbox":
        item = inbox.by_session(s.id) or {}
        source = {"telegram": "手機（Telegram）"}.get(item.get("source", ""), f"Inbox（{item.get('source', '?')}）")
        title = item.get("title", "")
        link = f"/ui#/inbox/{item['id']}" if item.get("id") else f"/ui#/chat/{s.id}?as=inbox"
        return (f"{source}：{title[:60]}" if title else source), link
    if s.user == "webui":
        return "主控台", f"/ui#/chat/{s.id}"
    if s.user == "workflow":
        return "排程（每日計畫等）", f"/ui#/chat/{s.id}?as={s.user}"
    if s.user.startswith("mcp"):
        caller = s.user.split(":", 1)[1] if ":" in s.user else "MCP"
        return f"MCP 呼叫端 {caller}", f"/ui#/chat/{s.id}?as={s.user}"
    return s.user, f"/ui#/chat/{s.id}?as={s.user}"


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _snippet(text: str, terms: list[str], width: int = 160) -> str:
    low = text.lower()
    at = min((i for i in (low.find(t) for t in terms) if i >= 0), default=0)
    start = max(0, at - width // 3)
    piece = " ".join(text[start:start + width].split())
    return ("…" if start else "") + piece + ("…" if start + width < len(text) else "")


def search(query: str = "", days: float = 30, limit: int = 8, _origin: dict | None = None) -> str:
    """meeseek's past conversations about `query`, best match first, then newest."""
    terms = [t.lower() for t in (query or "").split() if t.strip()]
    limit = min(max(int(limit or 8), 1), 20)
    here = (_origin or {}).get("session_id")
    hits = []
    for s in _sessions(days):
        if s.id == here or not _visible(s, _origin):
            continue   # the conversation asking is not the one it is looking for
        said = "\n".join(s.asked + s.replies)
        low = said.lower()
        if terms and not all(t in low for t in terms):
            continue
        texts = {k: "\n".join(getattr(s, k)).lower() for k in _WEIGHT}
        score = sum(w * min(texts[k].count(t), _CAP) for k, w in _WEIGHT.items() for t in terms)
        hits.append((score, s.last, s, "\n".join(s.typed + s.replies + s.auto)))
    if not hits:
        return (f"過去 {days:g} 天沒有提到「{query}」的 meeseek 對話。換個關鍵字（人名、MR 號、ticket key），"
                "或把 days 調大再找。" if terms else f"過去 {days:g} 天沒有 meeseek 對話。")
    hits.sort(key=lambda h: (h[0], h[1]), reverse=True)
    out = [f"**{len(hits)} 個 meeseek 對話**" + (f"提到「{query}」" if terms else "")
           + (f"，列出前 {limit} 個" if len(hits) > limit else "") + "：\n"]
    for _, _, s, said in hits[:limit]:
        where, link = _where(s)
        out.append(f"- **{_when(s.last)}** · {where} · {len(s.asked)} 則訊息 · session `{s.id}` · 開啟：{link}")
        out.append(f"  - 一開始問：{' '.join((s.typed or s.asked)[0].split())[:160]}")
        if terms:
            out.append(f"  - 提到的地方：{_snippet(said, terms)}")
    out.append("\n要看其中一個的內容，用 read_meeseek_session(session_id=…)。")
    return "\n".join(out)


def read(session_id: str, turns: int = 20, _origin: dict | None = None) -> str:
    """One past conversation: what the user asked and what meeseek answered, the last `turns` exchanges."""
    session_id = (session_id or "").strip()
    turns = min(max(int(turns or 20), 1), 60)
    found = [s for s in _sessions(MAX_DAYS) if s.id == session_id and _visible(s, _origin)]
    if not found:
        return (f"找不到 session `{session_id}`（只保留最近 {MAX_DAYS} 天）。用 search_meeseek_sessions 找正確的 id。")
    s = found[0]
    where, link = _where(s)
    lines = [f"**meeseek 對話** · {where} · {_when(s.first)} – {_when(s.last)} · 開啟：{link}\n"]
    from meeseek.runtime import history

    exchanges: list[list[str]] = []
    for r in history.rows(session=s.id, users=(s.user,)):
        event = r.event
        if event.get("partial"):
            continue
        author = event.get("author") or ""
        parts = (event.get("content") or {}).get("parts") or []
        text = "".join(p.get("text") or "" for p in parts if not p.get("thought")).strip()
        calls = [p["function_call"]["name"] for p in parts if p.get("function_call")]
        if author == "user" and text:
            exchanges.append([f"**使用者**：{_unwrap(text)[:2000]}"])
        elif exchanges and calls:
            if exchanges[-1][-1].startswith("（查了 "):   # one line for a run of lookups
                exchanges[-1][-1] = exchanges[-1][-1][:-1] + f", {', '.join(calls)}）"
            else:
                exchanges[-1].append(f"（查了 {', '.join(calls)}）")
        elif exchanges and text:
            exchanges[-1].append(f"**meeseek**（{author}）：{text[:3000]}")
    if len(exchanges) > turns:
        lines.append(f"（前面還有 {len(exchanges) - turns} 段，只列最後 {turns} 段）\n")
    for ex in exchanges[-turns:]:
        lines.append("\n".join(ex) + "\n")
    return "\n".join(lines)
