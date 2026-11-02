"""The drift report: where the knowledge base's documents and the code disagree.

On `triggers.drift_cron` (Monday 09:00 by default) the job gathers — read only,
and bounded in size:

    docs         the knowledge base's design and engineering-management documents
                 (`triggers.drift_docs`, else every Drive document but meeting
                 notes), the newest first, at most MAX_DOCS: path, date, headings
    workstreams  the open workstreams' keys, so a finding can name its ticket or MR
    repo         the main repo (the gitlab pack's first project)

One autonomous turn is counted (`triggers.daily_turns`). Claude Code reads the
documents in the knowledge-base folder, read only, and the main repo's docs/ and
code through meeseek's plugins (get_file, search_code). Each inconsistency it
finds (what the document says, what the code or docs/ say, where) goes into ONE
Inbox item, "Drift report — <date>"; a week with none files nothing. An
inconsistency that names an open workstream's ticket or MR is also noted on that
workstream. The report changes nothing: which side is right is the person's call.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from meeseek.app import app
from meeseek.config.paths import home
from meeseek.context import prompts
from meeseek.runtime import inbox

logger = logging.getLogger(__name__)

SOURCE = "drift"
MAX_DOCS = 12
HEADINGS = 20             # headings a document lists
MAX_WORKSTREAMS = 20
BUNDLE_CHARS = 16_000
MAX_FINDINGS = 40
_MEETING = re.compile(r"例會|會議|筆記|Transcript|meeting|notes", re.I)
_UPDATED = re.compile(r"文件最後更新[^0-9]*(\d{4}-\d{2}-\d{2})")
_HEADING = re.compile(r"^#{1,4}\s+(.+?)\s*$", re.M)
_FINDINGS = re.compile(r"```json\s*\n(.*?)```", re.S)


# --- gathering ------------------------------------------------------------------

def _drive() -> Path:
    return home().sources / "drive"


def _docs() -> list[Path]:
    """The documents to compare: the configured ones, else every Drive document
    but meeting notes — the newest first, at most MAX_DOCS."""
    from meeseek.config import cfg

    root = _drive()
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("*.md") if p.is_file()]
    wanted = [w.lower() for w in cfg.triggers.drift_docs if w.strip()]
    if wanted:
        files = [p for p in files if any(w in p.relative_to(root).as_posix().lower() for w in wanted)]
    else:
        files = [p for p in files if not _MEETING.search(p.name)]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_DOCS]


def _doc_row(p: Path) -> dict:
    text = p.read_text(encoding="utf-8", errors="replace")
    m = _UPDATED.search(text[:2000])
    updated = m.group(1) if m else datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
    heads = [h[:80] for h in _HEADING.findall(text)][:HEADINGS]
    return {"path": p.relative_to(home().kb).as_posix(), "updated": updated, "chars": len(text),
            "headings": heads}


def _workstreams() -> list[dict]:
    from meeseek.runtime import workstreams

    return [{"key": r["key"], "aliases": r["aliases"], "title": r["title"][:80]}
            for r in workstreams.rows()[:MAX_WORKSTREAMS]]


def _repo() -> str:
    from meeseek.runtime import workstreams

    return workstreams._default_repo()


def gather() -> dict:
    """What the report compares; read only."""
    bundle: dict = {"repo": "", "docs": [], "workstreams": []}
    try:
        bundle["repo"] = _repo()
    except Exception as e:  # noqa: BLE001
        logger.warning("[drift] the main repo: %s", e)
    for p in _docs():
        try:
            bundle["docs"].append(_doc_row(p))
        except OSError as e:
            logger.warning("[drift] reading %s: %s", p, e)
    try:
        bundle["workstreams"] = _workstreams()
    except Exception as e:  # noqa: BLE001 — the report goes without them
        logger.warning("[drift] reading the workstreams: %s", e)
    return bundle


def render(bundle: dict) -> str:
    """The bundle as Claude reads it, at most BUNDLE_CHARS."""
    parts = [f"Main repo: {bundle['repo'] or '(none configured: compare against docs/ you can read, or say so)'}",
             f"### documents ({len(bundle['docs'])})",
             *(json.dumps(d, ensure_ascii=False) for d in bundle["docs"]),
             f"### open workstreams ({len(bundle['workstreams'])})",
             *(json.dumps(w, ensure_ascii=False) for w in bundle["workstreams"])]
    text = "\n".join(parts)
    return text if len(text) <= BUNDLE_CHARS else text[:BUNDLE_CHARS] + "\n…(cut)"


# --- the run ------------------------------------------------------------------------

def install() -> None:
    """At server start: listen for the Claude sessions this module starts."""
    if _on_claude_turn not in app().after_claude_turn:
        app().after_claude_turn.append(_on_claude_turn)


def _day(now: float) -> str:
    return datetime.fromtimestamp(now).astimezone().strftime("%Y-%m-%d")


def run(now: float | None = None) -> dict | None:
    """Gather and hand the comparison to Claude; the Claude record, or None
    when there is nothing to compare, or the day's budget is spent."""
    from meeseek.config import cfg
    from meeseek.runtime import claude_agent, turn_budget

    now = now or time.time()
    bundle = gather()
    if not bundle["docs"]:
        logger.info("[drift] no knowledge-base documents to compare")
        return None
    if turn_budget.cap_reached():
        logger.info("[drift] skipped: the day's autonomous turns are spent (triggers.daily_turns)")
        return None
    if not cfg.claude.enabled:
        logger.info("[drift] skipped: Claude Code is off (claude.enabled)")
        return None
    day = _day(now)
    if inbox.by_key(f"drift:{day}"):
        logger.info("[drift] %s is filed already", day)
        return None
    install()
    turn_budget.count_turn()
    return claude_agent.start(prompts.text("runtime/drift_report#report", bundle=render(bundle)),
                              cwd=str(home().kb), origin={"drift": day}, read_only=True)


def parse(reply: str) -> list[dict] | None:
    """The inconsistencies from the last ```json block of Claude's reply; None
    when there is no readable block (the comparison did not finish)."""
    blocks = _FINDINGS.findall(reply or "")
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
    except ValueError:
        return None
    out = []
    for f in data if isinstance(data, list) else []:
        if not (isinstance(f, dict) and f.get("document_says") and f.get("code_says")):
            continue
        row = {k: str(f.get(k) or "")[:n] for k, n in (
            ("topic", 120), ("document", 200), ("document_says", 600), ("code_says", 600), ("where", 400))}
        refs = f.get("refs") or []
        row["refs"] = [str(r)[:120] for r in (refs if isinstance(refs, list) else [refs])][:4]
        out.append(row)
    return out[:MAX_FINDINGS]


def body(findings: list[dict]) -> str:
    lines = []
    for n, f in enumerate(findings, 1):
        lines += [f"{n}. **{f['topic'] or f['document']}**",
                  f"   - Document ({f['document']}): {f['document_says']}",
                  f"   - Code / docs/: {f['code_says']}",
                  f"   - Where: {f['where']}"]
    return "\n".join(lines)


def _note_workstreams(findings: list[dict], day: str) -> int:
    """Each inconsistency that names an open workstream's ticket or MR, on it."""
    from meeseek.runtime import workstreams

    n = 0
    for f in findings:
        text = " ".join([*f["refs"], f["topic"], f["document_says"], f["code_says"], f["where"]])
        for key in workstreams.keys_in(text):
            ws = workstreams.find(key)
            if ws and ws.get("status") == "open":
                workstreams.note(ws, f"Drift report {day}: {f['topic'] or f['document']} — the document says "
                                     f"{f['document_says'][:120]}; the code says {f['code_says'][:120]}",
                                 "drift report")
                n += 1
    return n


def finish(record: dict) -> dict | None:
    """Claude's comparison, onto the Inbox: one item listing each inconsistency,
    none when there is none. A comparison that did not finish says so."""
    from meeseek.runtime import claude_agent

    day = (record.get("origin") or {}).get("drift", "")
    reply = claude_agent.last_reply(record)
    findings = None if record.get("status") == "failed" else parse(reply)
    if findings == []:
        logger.info("[drift] %s: no inconsistencies", day)
        return None
    item = inbox.file(source=SOURCE, kind=SOURCE, key=f"drift:{day}", title=f"Drift report — {day}",
                      sender="meeseek", meta={"record": record.get("id", "")})
    if item is None:
        return None
    if findings is None:
        item["status"] = "failed"
        item["error"] = "The comparison did not finish: " + (reply[:500] or "Claude gave no list")
    else:
        item["body"] = body(findings)
        item["reply"] = _FINDINGS.sub("", reply).strip() or f"{len(findings)} inconsistencies."
        item["meta"].update(found=len(findings), noted=_note_workstreams(findings, day))
        item["status"] = "ready"
    inbox.save(item)
    return item


async def _on_claude_turn(record: dict) -> None:
    if (record.get("origin") or {}).get("drift"):
        finish(record)


def main() -> None:
    """`python -m meeseek.runtime.drift_report`: print what the report would compare now; changes nothing."""
    print(render(gather()))


if __name__ == "__main__":
    main()
