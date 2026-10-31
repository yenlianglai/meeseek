"""The Google pack's sources: what arrived on its own, for meeseek's trigger loop.

Each is called with `since` (epoch seconds) and the `state` it handed back
last time, and answers {"items": [...], "state": {...}} (meeseek_pack_kit).
An item's `group` is its conversation — a Chat space, a mail thread — and
`answered` lists [{group, at}] where you wrote yourself since: meeseek keeps
one item per conversation and settles it once you have replied.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr

from meeseek_pack_google.auth import get_credentials
from meeseek_pack_google.gdrive import build_service
from meeseek_pack_google.gmail import _extract_body, _headers
from meeseek_pack_google.gmail import _svc as _gmail_svc
from meeseek_pack_google.gchat import _svc as _chat_svc

_BODY_CHARS = 4000
_NO_REPLY = re.compile(r"no-?reply|do-?not-?reply|notifications?@|mailer-daemon|calendar-notification",
                       re.IGNORECASE)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# --- mail -------------------------------------------------------------------------

def _gmail_body(payload: dict) -> str:
    return _extract_body(payload).strip()


def _is_bulk(headers: dict) -> bool:
    """Mail no person is waiting on an answer to: lists, bulk, no-reply senders."""
    if "list-unsubscribe" in headers or "list-id" in headers:
        return True
    if headers.get("precedence", "").lower() in ("bulk", "list", "junk"):
        return True
    if headers.get("auto-submitted", "no").lower() != "no":
        return True
    return bool(_NO_REPLY.search(parseaddr(headers.get("from", ""))[1]))


def _is_invite(payload: dict) -> bool:
    """A calendar invitation or update: the calendar trigger covers the meeting."""
    if payload.get("mimeType") == "text/calendar":
        return True
    return any(_is_invite(p) for p in payload.get("parts") or [])


def gmail_poll(since: float = 0, state: dict | None = None,
               query: str = "in:inbox -from:me -category:promotions -category:social -category:forums") -> dict:
    """New mail matching `query` as trigger items (kind mail, one group per
    thread), and the threads you have written in since."""
    now = time.time()
    since = since or now - 12 * 3600
    svc = _gmail_svc()
    ids = [m["id"] for m in svc.users().messages().list(userId="me", q=f"{query} after:{int(since)}",
                                                       maxResults=25).execute().get("messages", [])]
    out = []
    for mid in ids:
        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        received = int(msg.get("internalDate", "0")) / 1000
        if received < since:
            continue
        h = _headers(msg)
        out.append({
            "kind": "mail", "key": f"gmail:{mid}", "group": f"thread:{msg['threadId']}",
            "title": h.get("subject", "(no subject)"),
            "sender": h.get("from", ""), "received_at": received,
            "link": f"https://mail.google.com/mail/u/0/#all/{msg['threadId']}",
            "body": (_gmail_body(msg.get("payload", {})) or msg.get("snippet", ""))[:_BODY_CHARS],
            "meta": {"thread_id": msg["threadId"], "bulk": _is_bulk(h),
                     "invite": _is_invite(msg.get("payload", {}))},
        })
    return {"items": out, "answered": _sent(svc, since), "state": state or {}}


def _sent(svc, since: float) -> list[dict]:
    """The threads you wrote in since `since`: [{group, at}]."""
    out: dict[str, float] = {}
    for m in svc.users().messages().list(userId="me", q=f"in:sent after:{int(since)}",
                                         maxResults=50).execute().get("messages", []):
        msg = svc.users().messages().get(userId="me", id=m["id"], format="minimal").execute()
        at = int(msg.get("internalDate", "0")) / 1000
        if at >= since:
            group = f"thread:{msg['threadId']}"
            out[group] = max(out.get(group, 0.0), at)
    return [{"group": g, "at": at} for g, at in out.items()]


# --- chat -------------------------------------------------------------------------

def _chat_self(svc, spaces: list[dict], state: dict) -> str:
    """Your Chat user id: the one sender found in every direct message. Kept
    once known (reading memberships would need another scope)."""
    if state.get("me"):
        return state["me"]
    counts: dict[str, int] = {}
    dms = [s for s in spaces if s.get("spaceType") == "DIRECT_MESSAGE"][:8]
    for sp in dms:
        msgs = svc.spaces().messages().list(parent=sp["name"], pageSize=20).execute().get("messages", [])
        for sender in {(m.get("sender") or {}).get("name") for m in msgs} - {None}:
            counts[sender] = counts.get(sender, 0) + 1
    if not counts:
        return ""
    me, n = max(counts.items(), key=lambda kv: kv[1])
    if n >= 2:  # one DM cannot tell you from the other person
        state["me"] = me
        return me
    return ""


def _mentions(message: dict, me: str) -> bool:
    return any(a.get("type") == "USER_MENTION"
               and (a.get("userMention") or {}).get("user", {}).get("name") == me
               for a in message.get("annotations") or [])


def chat_poll(since: float = 0, state: dict | None = None) -> dict:
    """Direct messages and @mentions since the last poll, one item (kind chat)
    per space, and the spaces you wrote in since. `state` remembers your Chat
    user id once known."""
    now = time.time()
    since = since or now - 12 * 3600
    state = dict(state or {})
    svc = _chat_svc()
    spaces, token = [], None
    while True:
        resp = svc.spaces().list(pageSize=200, pageToken=token).execute()
        spaces += resp.get("spaces", [])
        token = resp.get("nextPageToken")
        if not token:
            break
    me = _chat_self(svc, spaces, state)
    if not me:
        raise RuntimeError("Your Chat account is not known yet (it takes two direct messages with replies)")

    out, answered = [], []
    for sp in spaces:
        if _ts(sp.get("lastActiveTime", "")) <= since:
            continue
        dm = sp.get("spaceType") == "DIRECT_MESSAGE"
        msgs = svc.spaces().messages().list(
            parent=sp["name"], pageSize=50, orderBy="createTime asc",
            filter=f'createTime > "{_iso(since)}"').execute().get("messages", [])
        mine = [m for m in msgs if (m.get("sender") or {}).get("name") == me]
        if mine:
            answered.append({"group": sp["name"], "at": _ts(mine[-1].get("createTime", "")) or now})
        # For you since your last word there: a direct message, or a mention.
        after = _ts(mine[-1].get("createTime", "")) if mine else 0.0
        ms = [m for m in msgs if (m.get("sender") or {}).get("name") != me
              and (dm or _mentions(m, me)) and _ts(m.get("createTime", "")) > after]
        if not ms:
            continue
        last = ms[-1]
        who = (last.get("sender") or {}).get("displayName") or (last.get("sender") or {}).get("name", "")
        out.append({
            "kind": "chat", "key": f"chat:{last['name']}", "group": sp["name"],
            "title": (sp.get("displayName") or "DM") + ": " + (last.get("text") or "")[:60],
            "sender": who, "received_at": _ts(last.get("createTime", "")) or now,
            "link": sp.get("spaceUri", ""),
            "body": "\n".join(_said(m) for m in ms)[:_BODY_CHARS],
            "meta": {"space": sp["name"], "space_name": sp.get("displayName") or "", "dm": dm,
                     "thread": (last.get("thread") or {}).get("name", "")},
        })
    return {"items": out, "answered": answered, "state": state}


def _said(m: dict) -> str:
    """One message, with who said it — a space's item gathers several people's."""
    who = (m.get("sender") or {}).get("displayName") or ""
    text = m.get("text") or "(non-text message)"
    return f"{who}: {text}" if who else text


# --- calendar ---------------------------------------------------------------------

def calendar_poll(since: float = 0, state: dict | None = None, lead_minutes: int = 10) -> dict:
    """Meetings with other people starting within `lead_minutes` (kind meeting)."""
    now = time.time()
    svc = build_service("calendar", "v3", get_credentials())
    lead = lead_minutes * 60
    events = svc.events().list(calendarId="primary", timeMin=_iso(now), timeMax=_iso(now + lead),
                               singleEvents=True, orderBy="startTime").execute().get("items", [])
    out = []
    for ev in events:
        start = (ev.get("start") or {}).get("dateTime")
        attendees = [a for a in ev.get("attendees") or [] if not a.get("self")]
        me = next((a.get("displayName") or a.get("email", "") for a in ev.get("attendees") or []
                   if a.get("self")), "")
        if not start or not attendees or ev.get("status") == "cancelled":
            continue  # all-day, alone, or off
        if any(a.get("self") and a.get("responseStatus") == "declined" for a in ev.get("attendees") or []):
            continue
        key = f"calendar:{ev['id']}:{start}"
        out.append({
            "kind": "meeting", "key": key, "title": ev.get("summary", "(untitled meeting)"),
            "sender": (ev.get("organizer") or {}).get("email", ""), "received_at": now,
            "link": ev.get("hangoutLink") or ev.get("htmlLink", ""),
            "body": (ev.get("description") or "")[:_BODY_CHARS],
            "meta": {"start": start, "end": (ev.get("end") or {}).get("dateTime", ""),
                     "meet": ev.get("hangoutLink", ""), "me": me,
                     "attendees": [a.get("displayName") or a.get("email", "") for a in attendees]},
        })
    return {"items": out, "state": state or {}}
