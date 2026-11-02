"""The GitLab pack's source: MRs that need you, for meeseek's trigger loop.

`mr_poll` is called with `since` (epoch seconds) and the `state` it handed back
last time, and answers {"items", "answered", "state"} (meeseek_pack_kit). One
MR is one conversation (`group` "mr:<path>!<iid>"): an item for each new thing
there for you — a review asked of you, an MR assigned to you, someone else's
comment on an MR you review, are assigned or wrote — and `answered` where you
commented, approved, or the MR was merged or closed.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from meeseek_pack_gitlab.gitlab import _gitlab_get

_BODY_CHARS = 4000
_PER_ROLE = 50


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ts(iso: str | None) -> float:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _get(endpoint: str):
    out = _gitlab_get(endpoint)
    if isinstance(out, dict) and out.get("error"):
        raise RuntimeError(f"GitLab: {out['error']}")
    return out


def _me(state: dict) -> dict:
    """Your GitLab user {id, username}, kept once known."""
    if not state.get("me"):
        u = _get("user")
        state["me"] = {"id": u["id"], "username": u["username"]}
    return state["me"]


def _mrs(me: dict, since: float) -> dict[str, tuple[dict, str]]:
    """The MRs updated since that are yours to look at, and how: reviewer,
    assignee or author."""
    found: dict[str, tuple[dict, str]] = {}
    for role, param in (("reviewer", "reviewer_id"), ("assignee", "assignee_id"), ("author", "author_id")):
        for mr in _get(f"merge_requests?scope=all&state=all&{param}={me['id']}"
                       f"&updated_after={_iso(since)}&per_page={_PER_ROLE}"):
            found.setdefault(mr["references"]["full"], (mr, role))
    return found


def _asked_of_me(note: dict, me: dict) -> bool:
    """A system note that asks you: a review requested, the MR assigned to you."""
    body = note.get("body") or ""
    return bool(note.get("system")) and f"@{me['username']}" in body and (
        "review" in body or "assigned to" in body)


def _mine_counts(note: dict, me: dict) -> bool:
    """Your own word on the MR: a comment, or an approval."""
    if (note.get("author") or {}).get("id") != me["id"]:
        return False
    return not note.get("system") or "approved this merge request" in (note.get("body") or "")


def mr_poll(since: float = 0, state: dict | None = None) -> dict:
    """What on your MRs needs you since the last poll (kind mr, one group per
    MR), and the MRs you answered on or that were merged or closed."""
    now = time.time()
    since = since or now - 12 * 3600
    state = dict(state or {})
    me = _me(state)
    items, answered = [], []
    for ref, (mr, role) in _mrs(me, since).items():
        group = f"mr:{ref}"
        ended = max(_ts(mr.get("merged_at")), _ts(mr.get("closed_at")))
        if mr.get("state") in ("merged", "closed") and ended >= since:
            answered.append({"group": group, "at": ended, "why": mr["state"]})
        notes = [n for n in _get(f"projects/{mr['project_id']}/merge_requests/{mr['iid']}/notes"
                                 f"?sort=asc&order_by=created_at&per_page=100")
                 if _ts(n.get("created_at")) > since]
        mine = [n for n in notes if _mine_counts(n, me)]
        if mine:
            answered.append({"group": group, "at": _ts(mine[-1]["created_at"]), "why": "you"})
        after = _ts(mine[-1]["created_at"]) if mine else 0.0
        if mr.get("state") != "opened":
            continue
        news = [n for n in notes if _ts(n.get("created_at")) > after
                and (n.get("author") or {}).get("id") != me["id"]
                and (not n.get("system") or _asked_of_me(n, me))]
        opened = _ts(mr.get("created_at"))
        if not news and not (role != "author" and opened > max(since, after)):
            continue
        lines = [f"{(n.get('author') or {}).get('name', '?')}: {n.get('body', '')}" for n in news]
        if not news or opened > since or any(n.get("system") for n in news):   # new to you: what the MR is
            lines.insert(0, f"{mr['author']['name']} 開的 MR（你是 {role}）\n{mr.get('description') or ''}".strip())
        last = news[-1] if news else None
        items.append({
            "kind": "mr", "group": group,
            "key": f"mr:{ref}:{last['id'] if last else 'opened'}",
            "title": f"{ref} {mr['title']}"[:120],
            "sender": (last.get("author") or {}).get("name", "") if last else mr["author"]["name"],
            "received_at": _ts(last["created_at"]) if last else opened or now,
            "link": mr.get("web_url", ""),
            "body": "\n\n".join(lines)[:_BODY_CHARS],
            "meta": {"project": ref.split("!")[0], "iid": mr["iid"], "role": role,
                     "state": mr.get("state", ""), "draft": bool(mr.get("draft"))},
        })
    return {"items": items, "answered": answered, "state": state}
