"""Triggers — the sources that file Inbox items on their own.

Each source in project.yaml `triggers.sources` is a pack's source tool, asked
every minute (or its `every_s`) what arrived since the last poll — the Google
pack's mail, Chat messages and meetings about to start, the GCP pack's open
Cloud Monitoring incidents.

What an item is — its kind: mail, chat, mr, jira, alert, meeting — decides what happens
to it, whichever source filed it: bulk, no-reply mail and calendar invitations
are settled by what the source saw in their headers, no model asked.

A mail or a chat message is triaged by the fast model (reply / investigate /
fyi / ignore); only reply and investigate get an agent turn. An alert always
gets one (a first debug), and a meeting gets a brief. What each turn may do is
the Inbox's business (runtime/inbox.py).

`triggers.daily_turns` caps what a day may start — all sources together, and
a task's check turns with them (runtime/turn_budget.py counts the day): past
it an item is still filed, as fyi without a turn.

Some messages belong to another conversation than their own. A Jira or
GitLab notification mail about a ticket or an MR goes with that ticket's
(`jira:<KEY>`) or MR's (`mr:<path>!<iid>`) item, out of sight — the Jira and
GitLab sources say what there needs you. A Chat message that links one MR
files into that MR's item, which then gets a first-round review.

Each poll also lets go of what went stale (inbox.expire): a meeting's brief
once the meeting has ended, and a held write the digest has named.

The first poll of a source looks back FIRST_LOOKBACK_S, not further, so
turning a source on does not file a backlog. Where each source left off is
kept in state_file().
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parseaddr

from pydantic import BaseModel, Field

from meeseek.app import app
from meeseek.config import cfg
from meeseek.config.paths import home
from meeseek.context import prompts
from meeseek.runtime import inbox, turn_budget

logger = logging.getLogger(__name__)


def state_file() -> Path:
    return home().data / "triggers_state.json"


FIRST_LOOKBACK_S = 12 * 3600
# Each poll reaches this far back past the last one: items are deduplicated by
# key, so overlap costs nothing and a late-indexed message is not missed.
_OVERLAP_S = 120
_TRIAGE_BODY_CHARS = 2500

# Each source's last poll: {"ok", "error", "at", "filed"} — the Inbox page's header.


# --- state ------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    state_file().parent.mkdir(parents=True, exist_ok=True)
    state_file().write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _since(state: dict, source: str, now: float) -> float:
    return float(state.get(source, {}).get("since") or now - FIRST_LOOKBACK_S)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# --- triage -----------------------------------------------------------------------

class Triage(BaseModel):
    action: str = Field(description="reply / investigate / fyi / ignore")
    summary: str = Field(description="一句話的中文摘要")
    why: str = Field(description="一句判斷理由")


def triage(source: str, sender: str, title: str, body: str) -> dict:
    """reply / investigate / fyi / ignore, by the fast model. A failed call
    reads as fyi: the item is filed, nobody's time is spent on it."""
    from meeseek.runtime.oneshot import ask

    try:
        # Short: the whole poll waits on this, every source behind it.
        t = ask(prompts.text("internal/triage", source=source, sender=sender, title=title,
                             body=body[:_TRIAGE_BODY_CHARS]),
                model=cfg.models.fast, schema=Triage, temperature=0, timeout_s=60)
        action = t.action if t.action in ("reply", "investigate", "fyi", "ignore") else "fyi"
        return {"action": action, "summary": t.summary, "why": t.why}
    except Exception as e:  # noqa: BLE001
        logger.warning("triage failed: %s", e)
        return {"action": "fyi", "summary": "", "why": f"Triage failed: {type(e).__name__}"}


# --- a pack's source --------------------------------------------------------------

def _poll_pack(name: str, spec: dict, state: dict, now: float) -> list[dict]:
    """What the pack's source tool reports since the last poll, as candidates."""
    from meeseek.runtime import packs

    mine = state.setdefault(name, {})
    args = {**(spec.get("args") or {}), "since": _since(state, name, now), "state": mine.get("pack") or {}}
    text = packs.call(spec["tool"], args)
    data = json.loads(text or "{}")
    mine["pack"] = data.get("state") or {}
    mine["since"] = now - _OVERLAP_S
    # Where you wrote yourself since: what waited on you there is settled.
    for a in data.get("answered") or []:
        if a.get("group") and (n := inbox.answered(a["group"], float(a.get("at") or now))):
            logger.info("[triggers] %s: %d item(s) settled, you replied in %s", name, n, a["group"])
    keep = ("key", "title", "sender", "received_at", "link", "body", "meta", "kind", "group")
    return [{"source": name, **{k: item[k] for k in keep if k in item}}
            for item in data.get("items") or [] if item.get("key") and not inbox.by_key(item["key"])]


def enabled() -> list[str]:
    return list(cfg.triggers.sources)


def _poller(name: str):
    spec = cfg.triggers.sources[name]
    return lambda state, now: _poll_pack(name, spec, state, now)


def _every(name: str) -> float:
    return float(cfg.triggers.sources[name].get("every_s") or 0)


# --- which conversation a message belongs to ---------------------------------------

_JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_MR_URL = re.compile(r"https?://[^\s/]+/((?:[\w.-]+/)+[\w.-]+)/-/merge_requests/(\d+)")


def notice_group(candidate: dict) -> str:
    """The ticket or MR a Jira or GitLab notification mail is about, as its
    item's group; "" for any other mail. Jira Cloud sends from
    jira@<site>.atlassian.net with the key in the subject, GitLab from
    gitlab@<host> with the MR's URL in the body."""
    if inbox.kind_of(candidate) != "mail":
        return ""
    sender = parseaddr(candidate.get("sender", ""))[1].lower()
    local = sender.partition("@")[0]
    if local == "jira":
        m = _JIRA_KEY.search(candidate.get("title", ""))
        return f"jira:{m.group(1)}" if m else ""
    if local == "gitlab":
        m = _MR_URL.search(candidate.get("body", ""))
        return f"mr:{m.group(1)}!{m.group(2)}" if m else ""
    return ""


def chat_mr(candidate: dict) -> dict | None:
    """A Chat message that links exactly one MR (someone asking you to look
    at it): the candidate as an MR item in that MR's group. None otherwise."""
    if inbox.kind_of(candidate) != "chat":
        return None
    found = {(m.group(1), m.group(2)): m.group(0) for m in _MR_URL.finditer(candidate.get("body", ""))}
    if len(found) != 1:
        return None
    (path, iid), url = next(iter(found.items()))
    return {**candidate, "kind": "mr", "group": f"mr:{path}!{iid}", "link": url,
            "title": f"{path}!{iid}（{candidate.get('sender') or 'Chat'} 在 Chat 請你看）"[:120],
            "meta": {**(candidate.get("meta") or {}), "project": path, "iid": int(iid),
                     "role": "被請求 review（Chat）", "asked_in": "chat"}}


# --- turning a candidate into work ------------------------------------------------

def _local(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%m/%d %H:%M")
    except ValueError:
        return iso


def message_for(item: dict) -> str:
    """The request the item's first turn starts from."""
    common = prompts.text("runtime/trigger#common")
    meta, why = item["meta"], item["triage"].get("why", "")
    kind = inbox.kind_of(item)
    if kind == "mail":
        return prompts.text("runtime/trigger#gmail", common=common, why=why, sender=item["sender"],
                            title=item["title"], thread_id=meta["thread_id"], body=item["body"])
    if kind == "chat":
        where = "私訊" if meta.get("dm") else f"在「{meta.get('space_name') or meta['space']}」@了你"
        return prompts.text("runtime/trigger#chat", common=common, where=where, why=why,
                            sender=item["sender"], space=meta["space"], body=item["body"])
    if kind == "mr":
        return prompts.text("runtime/trigger#mr", common=common, why=why, role=meta.get("role", ""),
                            title=item["title"], link=item["link"], sender=item["sender"], body=item["body"])
    if kind == "jira":
        return prompts.text("runtime/trigger#jira", common=common, why=why, role=meta.get("role", ""),
                            title=item["title"], link=item["link"], sender=item["sender"], body=item["body"])
    if kind == "alert":
        return prompts.text("runtime/trigger#alert", common=common, title=item["title"],
                            project=meta["project"], opened=_local(meta["opened"]),
                            subject=meta["subject"], policy=meta["policy"])
    minutes = max(0, round((_ts(meta["start"]) - time.time()) / 60))
    from meeseek.context import ontology
    from meeseek.runtime.meeting_coach import coached, focus_note, practice_note

    if coaching := coached(item["title"]):
        return prompts.text("runtime/trigger#rehearsal", common=common, minutes=minutes,
                            title=item["title"], start=_local(meta["start"]), end=_local(meta["end"]),
                            attendees="、".join(meta["attendees"]) or "（無）",
                            me=meta.get("me") or "使用者本人",
                            body=item["body"] or "（沒有議程說明）",
                            practice=practice_note(item["title"]) or "（還沒有會後報告）",
                            focus=focus_note(coaching.focus), team=ontology.summary()[:4000] or "（還沒有）")
    return prompts.text("runtime/trigger#calendar", common=common, minutes=minutes,
                        title=item["title"], start=_local(meta["start"]), end=_local(meta["end"]),
                        attendees="、".join(meta["attendees"]) or "（無）",
                        body=item["body"] or "（沒有議程說明）")


def decide(candidate: dict) -> dict:
    """The triage an item is filed with. Alerts and meetings need no model to
    know they are work; bulk mail needs none to know it is not."""
    kind = inbox.kind_of(candidate)
    if kind == "alert":
        return {"action": "investigate", "summary": candidate["title"], "why": "Alert fired"}
    if kind == "meeting":
        return {"action": "brief", "summary": candidate["title"], "why": "Meeting about to start"}
    if kind == "mr" and candidate["meta"].get("asked_in") == "chat":
        return {"action": "investigate", "summary": candidate["title"], "why": "Review asked in Chat"}
    if candidate["meta"].get("bulk"):
        return {"action": "ignore", "summary": "", "why": "Bulk or automated mail"}
    if candidate["meta"].get("invite"):
        return {"action": "fyi", "summary": candidate["title"], "why": "Calendar invitation"}
    return triage(candidate["source"], candidate["sender"], candidate["title"], candidate["body"])


async def _take(candidate: dict) -> dict | None:
    """File one candidate, triage it, start its turn if it needs one. A
    message in a conversation that has an item already updates that item."""
    if inbox.by_key(candidate["key"]):
        return None
    if notice := notice_group(candidate):
        return _notice(candidate, notice)
    candidate = chat_mr(candidate) or candidate
    group = inbox.group_of(candidate)
    if earlier := inbox.by_group(group):
        return await _add_to(earlier[0], candidate)
    item = inbox.file(**{**candidate, "group": group})
    if item is None:
        return None
    item["triage"] = await asyncio.to_thread(decide, candidate)
    action = item["triage"]["action"]
    if action in ("ignore", "fyi"):
        item["status"] = action if action == "ignore" else "fyi"
        inbox.save(item)
        return item
    return _start(item)


def _notice(candidate: dict, group: str) -> dict | None:
    """A notification mail about a ticket or MR: folded into its item, or
    the item's first sign, as fyi, until the ticket's or MR's source files more."""
    if earlier := inbox.by_group(group):
        inbox.fold(earlier[0], candidate)
        return None
    item = inbox.file(**{**candidate, "group": group, "meta": {**candidate["meta"], "notice": True}})
    if item:
        item["status"] = "fyi"
        item["triage"] = {"action": "fyi", "summary": candidate["title"], "why": "Jira or GitLab notification"}
        inbox.save(item)
    return item


async def _add_to(item: dict, candidate: dict) -> dict:
    """A new message in an item's conversation. Still waiting on you: it shows
    the new message, and one that needs work gets a turn in the item's own
    session. Settled: it opens again as a new message would."""
    was = item["status"]
    triage = await asyncio.to_thread(decide, candidate)
    item = inbox.merge(item, candidate)
    action = triage["action"]
    if was in ("new", "working") or (was not in inbox.SETTLED and action in ("ignore", "fyi")):
        inbox.save(item)          # a turn is on it, or the news asks nothing more of you
        return item
    item["triage"] = triage
    if action in ("ignore", "fyi"):
        item["status"] = action
        inbox.save(item)
        return item
    return _start(item, {**item, "body": candidate.get("body", ""), "sender": candidate.get("sender", "")})


def _start(item: dict, about: dict | None = None) -> dict:
    """An agent turn on the item, within the day's cap; `about` is what the
    turn's message describes (the new message, for one added to an item)."""
    if turn_budget.cap_reached():
        cap = cfg.triggers.daily_turns
        logger.info("[triggers] %s filed as fyi: the day's %d autonomous turns are spent "
                    "(triggers.daily_turns)", item["key"], cap)
        item["triage"]["why"] += f"；已達每日 {cap} 次自主處理上限，只通知不處理"
        if item["status"] not in inbox.WAITING:    # one already waiting on you keeps waiting
            item["status"] = "fyi"
        inbox.save(item)
        return item
    item["message"] = message_for(about or item)
    inbox.save(item)
    turn_budget.count_turn()
    inbox.start(item["id"], item["message"])
    return item


async def poll(only: str = "") -> dict:
    """Poll every enabled source that is due (or `only` that one, now)."""
    state = _load_state()
    now = time.time()
    filed: list[dict] = []
    for name in ([only] if only else enabled()):
        last = app().trigger_status.get(name, {}).get("at", 0)
        if not only and now - last < _every(name):
            continue
        try:
            candidates = await asyncio.to_thread(_poller(name), state, now)
            taken = [i for i in [await _take(c) for c in candidates] if i]
            filed += taken
            app().trigger_status[name] = {"ok": True, "error": "", "at": now, "filed": len(taken)}
        except Exception as e:  # noqa: BLE001 — one broken source must not stop the rest
            logger.warning("[triggers] %s failed: %s", name, e)
            app().trigger_status[name] = {"ok": False, "error": _explain(e), "at": now, "filed": 0}
    _save_state(state)
    inbox.prune()
    inbox.expire(now, digest=bool(cfg.triggers.digest_cron and cfg.messengers.telegram
                                  and cfg.triggers.coach_telegram_chat))
    return {"filed": [i["id"] for i in filed], "status": app().trigger_status}


_DISABLED = re.compile(r"(\S+) API has not been used in project (\S+) before or it is disabled")


def _explain(e: Exception) -> str:
    """What to do about a source's failure, when the error says."""
    text = f"{type(e).__name__}: {e}"
    if "insufficient" in text.lower() and "scope" in text.lower():
        return "The login lacks a scope: run ./scripts/setup.sh to sign in again"
    if m := _DISABLED.search(text):
        return (f"The {m.group(1)} API is off in GCP project {m.group(2)} (project.yaml's quota "
                f"project): enable it under APIs & Services")
    return text[:300]
