"""A recorded meeting's action items, as Jira work held on its Inbox item.

After a meeting's notes are made (runtime/meeting_recorder.py), `propose`
reads its action items (prompts/internal/meeting_actions.md) and keeps
yours and your team's. Each is searched in Jira first — the tickets the notes
name, then a text search in the project — and the fast model decides
(prompts/internal/meeting_tickets.md): a comment on the ticket that already
has the work, a new ticket only when none does and the item is yours
(`create_for_team` for your team's too), or nothing. Each proposal is a held
action on the meeting's one item (inbox.hold): nothing is written until you
approve it, one by one, on the Inbox page.

Settings are project.yaml `triggers.meeting_tickets` (config/schema.py); with
no `project` it is off.

Why it is built so (2026-10-29, the 35 new tickets meetings had proposed: 2
approved, 13 rejected, 20 never answered):
  * 16 came from one meeting cut into 37 items, one per few minutes of
    recording — fixed in the recorder since (a meeting goes on in one item,
    as parts); this files every proposal on that one item.
  * 8 were for someone else and 19 had no assignee: only your items get a new
    ticket here, assigned to you; your team's get a comment on their ticket.
  * The agent created before searching — one meeting proposed the same new
    ticket twice, another a ticket for work an existing ticket had — and
    rewrote a ticket's whole description where a note was wanted: here the
    search comes first, and an existing ticket gets a comment, never an edit.
"""

from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field

from meeseek.config import cfg
from meeseek.context import prompts
from meeseek.runtime import inbox

logger = logging.getLogger(__name__)

AGENT = "meeting_tickets"
_PER_SEARCH = 5
_MAX_ACTIONS = 12
_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


class ActionItem(BaseModel):
    owner: str = ""
    whose: str = Field(default="other", description="me / team / other")
    what: str = ""
    due: str = ""
    context: str = ""
    keys: list[str] = Field(default_factory=list)
    terms: str = ""


class ActionItems(BaseModel):
    items: list[ActionItem] = Field(default_factory=list)


class Decision(BaseModel):
    index: int = Field(description="the action item's number")
    choice: str = Field(description="comment / create / skip")
    ticket: str = ""
    comment: str = ""
    summary: str = ""
    description: str = ""


class Decisions(BaseModel):
    decisions: list[Decision] = Field(default_factory=list)


def settings() -> dict:
    s = dict(cfg.triggers.meeting_tickets or {})
    s.setdefault("project", "")
    s.setdefault("components", "")
    s.setdefault("issue_type", "Task")
    s.setdefault("summary_prefix", "")
    s.setdefault("assignee", "")
    s.setdefault("team", "")
    s.setdefault("create_for_team", False)
    s.setdefault("create_tool", "jira_create_issue")
    s.setdefault("comment_tool", "jira_add_comment")
    s.setdefault("search_tool", "jira_search")
    return s


def enabled() -> bool:
    return bool(settings()["project"])


# --- Jira, read --------------------------------------------------------------------

def tickets_in(text: str) -> list[tuple[str, str]]:
    """[(key, summary)] from a search tool's answer: the Jira pack's Markdown
    ("- **KEY**: summary") or a JSON list of issues (mcp-atlassian)."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        data = data.get("issues", [])
    if isinstance(data, list):
        out = []
        for i in data:
            if isinstance(i, dict) and i.get("key"):
                summary = i.get("summary") or (i.get("fields") or {}).get("summary", "")
                out.append((i["key"], summary))
        return out
    return [(m.group(1), m.group(2).strip()) for m in re.finditer(r"\*\*([A-Z][A-Z0-9]+-\d+)\*\*:\s*(.*)", text or "")]


async def _search(jql: str) -> list[tuple[str, str]]:
    from meeseek.runtime import packs

    try:
        return tickets_in(await packs.acall(settings()["search_tool"], {"jql": jql, "max_results": _PER_SEARCH}))
    except Exception as e:  # noqa: BLE001 — a failed search leaves the item with fewer candidates
        logger.warning("[meeting_tickets] search failed (%s): %s", jql[:120], e)
        return []


async def candidates(action: ActionItem) -> list[tuple[str, str]]:
    """The tickets that may already have this work: the ones the notes name,
    then the project's open tickets matching its terms."""
    project = settings()["project"]
    found: dict[str, str] = {}
    keys = [k for k in action.keys if _KEY.fullmatch(k)]
    if keys:
        found.update(await _search(f"key in ({', '.join(keys)})"))
    terms = re.sub(r'["\\]', " ", action.terms or action.what).strip()
    if terms:
        jql = f'project = {project} AND statusCategory != Done AND text ~ "{terms}" ORDER BY updated DESC'
        for key, summary in await _search(jql):
            found.setdefault(key, summary)
    return list(found.items())[:_PER_SEARCH + len(keys)]


# --- the proposals -------------------------------------------------------------------

def _creatable(action: ActionItem) -> bool:
    return action.whose == "me" or (action.whose == "team" and bool(settings()["create_for_team"]))


def _write_args(action: ActionItem, d: Decision, found: list[tuple[str, str]]) -> tuple[str, dict] | None:
    """The held write for one decision; None when there is none to hold."""
    s = settings()
    if d.choice == "comment" and d.ticket in {k for k, _ in found} and d.comment.strip():
        return s["comment_tool"], {"issue_key": d.ticket, "body": d.comment.strip()}
    if d.choice == "create" and _creatable(action) and d.summary.strip():
        summary = d.summary.strip()
        if s["summary_prefix"] and not summary.startswith(s["summary_prefix"].strip()):
            summary = s["summary_prefix"] + summary
        args = {"project_key": s["project"], "summary": summary, "issue_type": s["issue_type"],
                "description": d.description.strip()}
        if s["components"]:
            args["components"] = s["components"]
        if action.whose == "me" and s["assignee"]:
            args["assignee"] = s["assignee"]
        return s["create_tool"], args
    return None


async def propose(item: dict, notes: str, *, title: str, date: str, me: str = "",
                  team_context: str = "") -> list[dict]:
    """Hold the meeting's Jira work on its item; the action items, each with
    what was proposed for it, are kept in its meta["action_items"]."""
    from meeseek.runtime.oneshot import aask

    if not enabled():
        return []
    s = settings()
    got = await aask(prompts.text("internal/meeting_actions", me=me or "使用者本人", team=s["team"] or "（沒有設定）",
                                  team_context=team_context[:4000] or "（沒有）", title=title, date=date,
                                  notes=notes[:30000]),
                     model=cfg.models.fast, schema=ActionItems, temperature=0, timeout_s=120)
    actions = [a for a in got.items if a.whose in ("me", "team") and a.what.strip()][:_MAX_ACTIONS]
    if not actions:
        return []
    found = [await candidates(a) for a in actions]
    listing = "\n\n".join(
        f"{n}. [{a.whose}] {a.owner} — {a.what}" + (f"（期限：{a.due}）" if a.due else "")
        + f"\n   can_create: {'true' if _creatable(a) else 'false'}\n   背景：{a.context}\n   既有票："
        + ("".join(f"\n   - {k}: {summary}" for k, summary in f) or "（沒找到）")
        for n, (a, f) in enumerate(zip(actions, found), 1))
    decided = await aask(prompts.text("internal/meeting_tickets", title=title, date=date,
                                      prefix=s["summary_prefix"] or "（不用前綴）", actions=listing),
                         model=cfg.models.fast, schema=Decisions, temperature=0, timeout_s=180)
    by_index = {d.index: d for d in decided.decisions}
    listed, seen = [], set()
    for n, (a, f) in enumerate(zip(actions, found), 1):
        d = by_index.get(n)
        write = _write_args(a, d, f) if d else None
        entry = {"owner": a.owner, "whose": a.whose, "what": a.what, "due": a.due,
                 "proposal": d.choice if write else "skip", "ticket": (d.ticket if d and write else ""),
                 "action_id": ""}
        if write and (sig := (write[0], json.dumps(write[1], sort_keys=True, ensure_ascii=False))) not in seen:
            seen.add(sig)
            item = inbox.get(item["id"]) or item
            entry["action_id"] = inbox.hold(item, write[0], write[1], AGENT)["id"]
        listed.append(entry)
    item = inbox.get(item["id"]) or item
    item["meta"]["action_items"] = listed
    inbox.save(item)
    return listed


def summary(listed: list[dict]) -> str:
    """The action items and what was proposed, for the meeting's turn."""
    if not listed:
        return "（沒有你或你團隊的待辦，或沒有設定 triggers.meeting_tickets）"
    said = {"comment": "提出在 {ticket} 留言", "create": "提出開新票", "skip": "不需要動 Jira"}
    return "\n".join(f"- [{'你' if e['whose'] == 'me' else '團隊'}] {e['owner']} — {e['what']}"
                     + (f"（{e['due']}）" if e["due"] else "") + "：" + said[e["proposal"]].format(ticket=e["ticket"])
                     for e in listed)
