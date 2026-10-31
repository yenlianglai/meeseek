"""The daily review: what went wrong today, handed to Claude Code to find the causes.

On `triggers.review_cron` (weekdays 18:30 by default) the job gathers the last
24 hours — read only, and bounded in size:

    log        the server's warnings and errors (log_file(), filled by install())
    turns      turns that failed, hit a cap, retried or ended on a model error
    feedback   your corrections, re-asks, 👎, rejected or edited writes, dismissals
    inbox      items that failed, were retried or were dismissed
    claude     Claude Code tasks that failed; wakes that failed
    sources    triggers and channels that are down (app().trigger_status, channel_status)

When nothing in the bundle is new since the last review, nothing is filed.
Otherwise one autonomous turn is counted (`triggers.daily_turns`), Claude Code
reads the bundle in meeseek's own folder, read only, and its ranked fixes
become ONE Inbox item, "Daily review — <date>". Each fix is a held action:
approved, it starts a Claude session that implements it in a new git worktree
on a branch of its own and commits there. Nothing is pushed.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path

from meeseek.app import app
from meeseek.config.paths import home
from meeseek.context import prompts
from meeseek.runtime import inbox
from meeseek.runtime.store import JsonlStore

logger = logging.getLogger(__name__)

SOURCE = "review"
WINDOW_S = 24 * 3600
LOG_MAX_BYTES = 2 * 1024 * 1024
SECTION_ROWS = 25          # rows a section keeps, newest or most frequent first
ROW_CHARS = 400            # characters a row keeps
BUNDLE_CHARS = 24_000      # the whole bundle, as Claude reads it
FIX_TOOL = "claude_fix"
_SECRET = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}|(?i:(?:token|key|secret|password)=)[^&\s]+")
_DIGITS = re.compile(r"[0-9a-f]{6,}|\d+")    # ids and counts: the same message all the same
_FIXES = re.compile(r"```json\s*\n(.*?)```", re.S)


def log_file() -> Path:
    return home().data / "logs" / "warnings.jsonl"


def state_file() -> Path:
    return home().data / "daily_review.json"


def worktrees_dir() -> Path:
    return home().data / "review-worktrees"


# --- the server log -------------------------------------------------------------

class _WarningLog(logging.Handler):
    """Warnings and errors, one JSON line each, so the review can read the day's."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                tail = traceback.format_exception(*record.exc_info)[-3:]
                msg += "\n" + "".join(tail).strip()
            JsonlStore(log_file(), max_bytes=LOG_MAX_BYTES).append(
                {"at": record.created, "level": record.levelname, "logger": record.name,
                 "msg": _SECRET.sub("[redacted]", msg)[:1500]})
        except Exception:  # noqa: BLE001 — a log line lost, never a crash
            pass


def install() -> None:
    """At server start: keep warnings and errors on disk, and listen for the
    Claude sessions this module starts."""
    root = logging.getLogger()
    if not any(isinstance(h, _WarningLog) for h in root.handlers):
        root.addHandler(_WarningLog())
    if _on_claude_turn not in app().after_claude_turn:
        app().after_claude_turn.append(_on_claude_turn)


# --- gathering ------------------------------------------------------------------

def _cut(text, n: int = ROW_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def _log(since: float) -> list[dict]:
    """Warnings and errors, the same message counted once (digits aside)."""
    groups: dict[str, dict] = {}
    for r in JsonlStore(log_file()).read():
        if r.get("at", 0) < since:
            continue
        head = (r.get("msg") or "").split("\n")[0]
        key = f"{r.get('logger')}|{_DIGITS.sub('#', head)[:160]}"
        g = groups.setdefault(key, {"key": key, "level": r.get("level"), "logger": r.get("logger"),
                                    "count": 0, "last": 0, "msg": ""})
        g["count"] += 1
        if r["at"] >= g["last"]:
            g["last"], g["msg"] = r["at"], _cut(r.get("msg"), 800)
        if r.get("level") in ("ERROR", "CRITICAL"):
            g["level"] = r["level"]
    return sorted(groups.values(), key=lambda g: (g["level"] != "ERROR", -g["count"]))[:SECTION_ROWS]


def _turns(since: float) -> list[dict]:
    from meeseek.runtime import trajectory

    out = []
    for t in trajectory.read(since):
        auto = t.get("auto") or {}
        flags = {k: v for k, v in auto.items()
                 if v and k in ("tool_errors", "cap_hits", "time_grace", "model_error", "empty_nudge")}
        if t.get("retries"):
            flags["retries"] = t["retries"]
        if not flags:
            continue
        failed = sorted({c[0] for c in t.get("calls", []) if len(c) > 1 and c[1]})
        out.append({"key": f"turn:{t.get('inv')}", "inv": t.get("inv"), "session": t.get("session"),
                    "skill": t.get("skill") or "", "at": t.get("at", 0), "flags": flags,
                    "failed_tools": failed, "request": _cut(t.get("request"), 200),
                    "reply": _cut(t.get("reply_head"), 200)})
    return sorted(out, key=lambda r: -r["at"])[:SECTION_ROWS]


def _feedback(since: float) -> list[dict]:
    from meeseek.runtime import feedback

    out = []
    for o in feedback.read(since):
        kind = o.get("kind")
        if kind == "thumb" and (o.get("value") or 0) >= 0:
            continue
        if kind not in ("correction", "reask", "thumb", "write_rejected", "write_edited", "dismissed"):
            continue
        said = o.get("text") or o.get("note") or o.get("reason") or ""
        out.append({"key": f"fb:{o.get('inv')}:{kind}:{o.get('at')}", "kind": kind, "at": o.get("at", 0),
                    "session": o.get("session"), "inv": o.get("inv"), "said": _cut(said),
                    **({"tool": o["tool"]} if o.get("tool") else {}),
                    **({"diff": _cut(json.dumps(o["diff"], ensure_ascii=False))} if o.get("diff") else {})})
    return sorted(out, key=lambda r: -r["at"])[:SECTION_ROWS]


def _inbox(since: float) -> list[dict]:
    out = []
    for i in inbox.items(1000):
        if i.get("updated_at", 0) < since or i.get("source") == SOURCE:
            continue
        retried = bool((i.get("meta") or {}).get("retried"))
        if i["status"] not in ("failed", "dismissed") and not retried:
            continue
        out.append({"key": f"inbox:{i['id']}:{i['status']}", "id": i["id"], "source": i.get("source"),
                    "status": i["status"], "retried": retried, "title": _cut(i.get("title"), 120),
                    "error": _cut(i.get("error")), "dismiss_reason": i.get("dismiss_reason", ""),
                    "at": i.get("updated_at", 0)})
    return sorted(out, key=lambda r: -r["at"])[:SECTION_ROWS]


def _claude(since: float) -> list[dict]:
    from meeseek.runtime import claude_agent, wakes

    out = []
    for r in claude_agent.records():
        if r.get("updated_at", 0) < since or r.get("status") != "failed":
            continue
        last = next((h for h in reversed(r.get("handoffs", [])) if h.get("from") == "claude"), {})
        out.append({"key": f"claude:{r['id']}", "what": "claude task", "title": _cut(r.get("title"), 120),
                    "error": _cut(last.get("error") or last.get("text")), "at": r["updated_at"]})
    for w in wakes.wake_store().read():
        if w.get("status") == "failed" and (w.get("settled_at") or 0) >= since:
            out.append({"key": f"wake:{w['id']}", "what": f"wake {w.get('kind')}", "title": _cut(w.get("why"), 120),
                        "error": _cut(w.get("note")), "at": w["settled_at"]})
    return sorted(out, key=lambda r: -r["at"])[:SECTION_ROWS]


def _sources() -> list[dict]:
    a = app()
    return [{"key": f"source:{name}:{_cut(s.get('error'), 80)}", "source": name, "error": _cut(s.get("error")),
             "at": s.get("at", 0)}
            for name, s in {**a.trigger_status, **a.channel_status}.items() if not s.get("ok", True)]


SECTIONS = ("log", "turns", "feedback", "inbox", "claude", "sources")


def gather(now: float | None = None) -> dict:
    """The last WINDOW_S of what went wrong; read only."""
    now = now or time.time()
    since = now - WINDOW_S
    readers = {"log": _log, "turns": _turns, "feedback": _feedback, "inbox": _inbox, "claude": _claude,
               "sources": lambda _since: _sources()}
    bundle: dict = {"since": since, "until": now}
    for name in SECTIONS:
        try:
            bundle[name] = readers[name](since)
        except Exception as e:  # noqa: BLE001 — one unreadable store leaves the others
            logger.warning("[review] reading %s: %s", name, e)
            bundle[name] = []
    return bundle


def keys(bundle: dict) -> set[str]:
    return {row["key"] for name in SECTIONS for row in bundle.get(name, [])}


def _seen() -> set[str]:
    try:
        return set(json.loads(state_file().read_text(encoding="utf-8")).get("keys", []))
    except (OSError, ValueError):
        return set()


def new_keys(bundle: dict) -> set[str]:
    """What the last review did not see already; empty = nothing new."""
    return keys(bundle) - _seen()


def render(bundle: dict) -> str:
    """The bundle as Claude reads it: one JSON section each, at most BUNDLE_CHARS."""
    when = lambda t: datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d %H:%M")  # noqa: E731
    parts = [f"Window: {when(bundle['since'])} – {when(bundle['until'])}"]
    room = BUNDLE_CHARS
    for left, name in zip(range(len(SECTIONS), 0, -1), SECTIONS):
        rows = [{k: (when(v) if k in ("at", "last") and v else v) for k, v in r.items() if k != "key"}
                for r in bundle.get(name, [])]
        text = f"### {name} ({len(rows)})\n" + "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        share = room // left      # what a short section leaves goes to the ones after it
        if len(text) > share:
            text = text[:share] + "\n…(cut)"
        room -= len(text)
        parts.append(text)
    return "\n\n".join(parts)


# --- the review -----------------------------------------------------------------

def _repo() -> Path:
    from meeseek.config import PROJECT_ROOT

    return PROJECT_ROOT


def run(now: float | None = None) -> dict | None:
    """Gather, and when something is new, hand it to Claude and file the item.
    The item, or None on a day with nothing new (or the day's budget spent)."""
    from meeseek.config import cfg
    from meeseek.runtime import claude_agent, turn_budget

    now = now or time.time()
    bundle = gather(now)
    fresh = new_keys(bundle)
    if not fresh:
        logger.info("[review] nothing new since the last review")
        return None
    if turn_budget.cap_reached():
        logger.info("[review] skipped: the day's autonomous turns are spent (triggers.daily_turns)")
        return None
    if not cfg.claude.enabled:
        logger.info("[review] skipped: Claude Code is off (claude.enabled)")
        return None
    day = datetime.fromtimestamp(now).astimezone().strftime("%Y-%m-%d")
    item = inbox.file(source=SOURCE, kind=SOURCE, key=f"review:{day}", title=f"Daily review — {day}",
                      sender="meeseek", body=render(bundle), meta={"new": len(fresh)})
    if item is None:
        logger.info("[review] %s is filed already", day)
        return None
    install()
    turn_budget.count_turn()
    item["status"] = "working"
    inbox.save(item)
    try:
        record = claude_agent.start(prompts.text("runtime/daily_review#review", bundle=item["body"]),
                                    cwd=str(_repo()), origin={"review_item": item["id"]}, read_only=True)
    except Exception as e:  # noqa: BLE001 — the item says why
        item["status"], item["error"] = "failed", str(e)
        inbox.save(item)
        return item
    item["meta"]["record"] = record["id"]
    inbox.save(item)
    state_file().write_text(json.dumps({"keys": sorted(keys(bundle)), "at": now}), encoding="utf-8")
    return item


def parse_fixes(reply: str) -> list[dict]:
    """The ranked fixes from the last ```json block of Claude's reply."""
    blocks = _FIXES.findall(reply or "")
    if not blocks:
        return []
    try:
        data = json.loads(blocks[-1])
    except ValueError:
        return []
    out = []
    for f in data if isinstance(data, list) else []:
        if isinstance(f, dict) and f.get("title") and f.get("fix"):
            out.append({"title": str(f["title"])[:120], "fix": str(f["fix"])[:2000],
                        "saves": str(f.get("saves", ""))[:120]})
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "fix"


def fill(item: dict, record: dict) -> dict:
    """Claude's review, onto the item: the ranked list, one held action per fix."""
    from meeseek.runtime import claude_agent

    reply = claude_agent.last_reply(record)
    if record.get("status") == "failed":
        item["status"], item["error"] = "failed", reply or "Claude did not finish the review"
        return item
    item["reply"] = _FIXES.sub("", reply).strip()
    day = item["key"].removeprefix("review:")
    for n, f in enumerate(parse_fixes(reply), 1):
        item["actions"].append({
            "id": f"{n:02d}{record['id'][:6]}", "tool": FIX_TOOL, "agent": "daily_review",
            "args": {"title": f["title"], "fix": f["fix"], "saves": f["saves"],
                     "branch": f"review/{day}-{n}-{_slug(f['title'])}"},
            "status": "pending", "result": "", "at": time.time(), "invocation_id": ""})
    _settle(item)
    item["seen"] = False
    return item


def _settle(item: dict) -> None:
    acts = item["actions"]
    if any(a["status"] == "pending" for a in acts):
        item["status"] = "needs_approval"
    elif any(a["status"] in ("approved", "running") for a in acts):
        item["status"] = "working"
    else:
        item["status"] = "ready"


async def carry_out(item_id: str) -> None:
    """The fixes you approved: each a Claude session on a branch of its own."""
    from meeseek.config import cfg
    from meeseek.runtime import claude_agent

    item = inbox.get(item_id)
    if item is None:
        return
    install()
    chat = cfg.triggers.coach_telegram_chat
    for a in item["actions"]:
        if a["status"] != "approved":
            continue
        try:
            folder = _worktree(a["args"]["branch"], a["id"])
            origin = {"review_item": item_id, "action": a["id"],
                      **({"channel": "telegram", "chat": chat} if chat else {})}
            record = claude_agent.start(prompts.text("runtime/daily_review#fix", title=a["args"]["title"],
                                                     fix=a["args"]["fix"], branch=a["args"]["branch"]),
                                        cwd=str(folder), origin=origin)
            a["status"], a["record"], a["result"] = "running", record["id"], f"Claude works in {folder}"
        except Exception as e:  # noqa: BLE001 — the action says why
            a["status"], a["result"] = "failed", str(e)[:500]
    _settle(item)
    inbox.save(item)


def _worktree(branch: str, action_id: str) -> Path:
    """A new git worktree of meeseek's repo on `branch`, from the current HEAD."""
    folder = worktrees_dir() / f"{branch.rsplit('/', 1)[-1]}-{action_id}"
    folder.parent.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(["git", "-C", str(_repo()), "worktree", "add", "-b", branch, str(folder), "HEAD"],
                         capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(f"git worktree add: {out.stderr.strip()[:300]}")
    return folder


WORKTREE_KEEP_DAYS = 14   # a fix's worktree outlives its last run this long; its branch and commit stay


def prune_worktrees(now: float | None = None) -> int:
    """Remove the fixes' worktrees not used for WORKTREE_KEEP_DAYS and not
    running; how many. The branches and their commits stay in the repo."""
    from meeseek.runtime import claude_agent

    root = worktrees_dir()
    if not root.is_dir():
        return 0
    cutoff = (now or time.time()) - WORKTREE_KEEP_DAYS * 86400
    last: dict[str, float] = {}
    busy: set[str] = set()
    for r in claude_agent.records():
        cwd = str(Path(r.get("cwd", "")).resolve())
        last[cwd] = max(last.get(cwd, 0.0), r.get("updated_at") or r.get("created_at") or 0.0)
        if claude_agent.running(r.get("id", "")):
            busy.add(cwd)
    n = 0
    for folder in root.iterdir():
        key = str(folder.resolve())
        if folder.is_dir() and key not in busy and max(last.get(key, 0.0), folder.stat().st_mtime) < cutoff:
            subprocess.run(["git", "-C", str(_repo()), "worktree", "remove", "--force", str(folder)],
                           capture_output=True, text=True, timeout=60)
            if folder.exists():
                import shutil

                shutil.rmtree(folder, ignore_errors=True)
            n += 1
    if n:
        subprocess.run(["git", "-C", str(_repo()), "worktree", "prune"], capture_output=True, text=True, timeout=60)
    return n


async def _on_claude_turn(record: dict) -> None:
    """A Claude session this module started ended: its review or its fix, onto the item."""
    origin = record.get("origin") or {}
    item = inbox.get(origin.get("review_item", "")) if origin.get("review_item") else None
    if item is None:
        return
    from meeseek.runtime import claude_agent

    if origin.get("action"):
        a = next((x for x in item["actions"] if x["id"] == origin["action"]), None)
        if a is None:
            return
        a["status"] = "failed" if record.get("status") == "failed" else "done"
        a["result"] = claude_agent.last_reply(record)[:inbox._RESULT_CHARS]
        _settle(item)
        inbox.save(item)
        return
    fill(item, record)
    inbox.save(item)
    await push(item)


async def push(item: dict) -> None:
    """The ranked list to the personal chat, then each fix with Approve / Reject.
    In the quiet hours it stays for the Inbox digest."""
    from meeseek.config import cfg
    from meeseek.runtime import messengers

    ch, chat = messengers.channels().get("telegram"), cfg.triggers.coach_telegram_chat
    if ch is None or not chat or messengers.quiet_until():
        return
    head = f"**{item['title']}**"
    if item["status"] == "failed":
        await ch.send_reply(chat, f"{head}（沒有完成）\n\n{item['error']}", [])
        return
    await ch.send_reply(chat, f"{head}\n\n{item['reply'] or '（沒有找到要修的）'}", [])
    for a in item["actions"]:
        if a["status"] == "pending" and not a.get("asked"):
            await ch.ask(chat, item, a)
            a["asked"] = True
    inbox.save(item)


def main() -> None:
    """`python -m meeseek.runtime.daily_review`: print what the review would gather now; changes nothing."""
    bundle = gather()
    print(render(bundle))
    print(f"\n{len(keys(bundle))} row(s), {len(new_keys(bundle))} new since the last review")


if __name__ == "__main__":
    main()
