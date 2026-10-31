"""The Jira pack's source: tickets that need you, for meeseek's trigger loop.

`jira_poll` is called with `since` (epoch seconds) and the `state` it handed
back last time, and answers {"items", "answered", "state"} (meeseek_pack_kit).
One ticket is one conversation (`group` "jira:<KEY>"): an item for each new
thing there for you — the ticket assigned to you, someone else's comment on a
ticket you are assigned, reported or watch, a comment that mentions you — and
`answered` where you commented or moved the ticket to another status.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import quote_plus

from meeseek_pack_jira.jira import _jira_get, _settings

_BODY_CHARS = 4000
_MAX_ISSUES = 50


def _ts(iso: str | None) -> float:
    """A Jira time ("2026-10-29T16:01:00.000+0800") as epoch seconds."""
    try:
        return datetime.strptime(iso or "", "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0


def _get(endpoint: str):
    out = _jira_get(endpoint)
    if isinstance(out, dict) and out.get("error"):
        raise RuntimeError(f"Jira: {out['error']}")
    return out


def _me(state: dict) -> dict:
    """Your Jira account {accountId, displayName}, kept once known."""
    if not state.get("me"):
        u = _get("myself")
        state["me"] = {"accountId": u["accountId"], "displayName": u.get("displayName", "")}
    return state["me"]


def _text(adf) -> str:
    """The plain text of an Atlassian Document Format body (a mention as @name)."""
    if isinstance(adf, str):
        return adf
    if not isinstance(adf, dict):
        return ""
    if adf.get("type") == "text":
        return adf.get("text", "")
    if adf.get("type") == "mention":
        return (adf.get("attrs") or {}).get("text") or "@?"
    inner = "".join(_text(c) for c in adf.get("content") or [])
    return inner + ("\n" if adf.get("type") in ("paragraph", "heading", "listItem") else "")


def _mentions(adf, account_id: str) -> bool:
    if not isinstance(adf, dict):
        return False
    if adf.get("type") == "mention" and (adf.get("attrs") or {}).get("id") == account_id:
        return True
    return any(_mentions(c, account_id) for c in adf.get("content") or [])


def _jira_time(ts: float) -> str:
    """JQL's time literal, in the server's zone (UTC here: Jira takes the user's)."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y/%m/%d %H:%M")


def _issues(me: dict, since: float) -> list[dict]:
    """Tickets updated since that are yours to look at: assigned to you,
    reported by you, watched by you, or naming you in their text."""
    name = me["displayName"].replace('"', '\\"')
    mine = "assignee = currentUser() OR reporter = currentUser() OR watcher = currentUser()"
    if name:
        mine += f' OR text ~ "\\"{name}\\""'
    jql = f'({mine}) AND updated >= "{_jira_time(since)}" ORDER BY updated ASC'
    data = _get(f"search/jql?jql={quote_plus(jql)}&fields=summary,status,assignee,reporter,created"
                f"&maxResults={_MAX_ISSUES}")
    return data.get("issues", [])


def _changes(key: str, since: float) -> list[dict]:
    """The ticket's changes since: [{at, author, field, to}] (assignee, status)."""
    out = []
    for h in _get(f"issue/{key}/changelog?maxResults=100").get("values", []):
        at = _ts(h.get("created"))
        if at <= since:
            continue
        for it in h.get("items") or []:
            if it.get("field") in ("assignee", "status"):
                out.append({"at": at, "author": (h.get("author") or {}).get("accountId", ""),
                            "field": it["field"], "to": it.get("to") or "", "to_name": it.get("toString") or ""})
    return out


def jira_poll(since: float = 0, state: dict | None = None) -> dict:
    """What on your Jira tickets needs you since the last poll (kind jira, one
    group per ticket), and the tickets you commented on or moved."""
    now = time.time()
    since = since or now - 12 * 3600
    state = dict(state or {})
    me = _me(state)
    base = _settings().get("base_url", "")
    items, answered = [], []
    for issue in _issues(me, since):
        key, f = issue["key"], issue.get("fields") or {}
        group = f"jira:{key}"
        comments = [c for c in _get(f"issue/{key}/comment?orderBy=created&maxResults=100").get("comments", [])
                    if _ts(c.get("created")) > since]
        changes = _changes(key, since)
        mine = [_ts(c["created"]) for c in comments if (c.get("author") or {}).get("accountId") == me["accountId"]]
        mine += [ch["at"] for ch in changes if ch["author"] == me["accountId"] and ch["field"] == "status"]
        if mine:
            answered.append({"group": group, "at": max(mine)})
        after = max(mine, default=0.0)
        assigned = (f.get("assignee") or {}).get("accountId") == me["accountId"]
        role = "指派給你" if assigned else "你回報的" if (f.get("reporter") or {}).get(
            "accountId") == me["accountId"] else "你關注或被提到的"
        news = [c for c in comments if _ts(c.get("created")) > after
                and (c.get("author") or {}).get("accountId") != me["accountId"]
                and (assigned or role == "你回報的" or _mentions(c.get("body"), me["accountId"]))]
        given = [ch for ch in changes if ch["field"] == "assignee" and ch["to"] == me["accountId"]
                 and ch["author"] != me["accountId"] and ch["at"] > after]
        created = _ts(f.get("created"))
        new_to_me = assigned and created > since and (f.get("reporter") or {}).get("accountId") != me["accountId"]
        if not news and not given and not new_to_me:
            continue
        lines = [f"{(c.get('author') or {}).get('displayName', '?')}: {_text(c.get('body')).strip()}" for c in news]
        if given or new_to_me:
            lines.insert(0, f"這張票指派給你了（狀態：{(f.get('status') or {}).get('name', '?')}）")
        last = news[-1] if news else None
        times = ([_ts(last["created"])] if last else []) + [g["at"] for g in given]
        at = max(times) if times else created or now
        items.append({
            "kind": "jira", "group": group,
            "key": f"jira:{key}:{last['id'] if last else 'assigned-' + str(int(at))}",
            "title": f"{key} {f.get('summary', '')}"[:120],
            "sender": (last.get("author") or {}).get("displayName", "") if last else (
                f.get("reporter") or {}).get("displayName", ""),
            "received_at": at,
            "link": f"{base}/browse/{key}" if base else "",
            "body": "\n\n".join(lines)[:_BODY_CHARS],
            "meta": {"issue_key": key, "role": role, "status": (f.get("status") or {}).get("name", "")},
        })
    return {"items": items, "answered": answered, "state": state}
