"""The goldfish review of a spec: reviewers that remember nothing.

Three reviewers read a document with no context but the document itself and
the files and links it references — never the conversation that wrote it. Each
is one model call (`spec_review.model`, else models.workflow) with its own lens:

    comprehension   can a reader restate the goal, the scope and the plan unaided?
    zoom_out        is it as simple as it can be: nothing built before it is needed,
                    nothing built twice that already exists?
    readiness       can an engineer implement it now: paths, signatures, data,
                    tests, done criteria?

Each returns findings {severity: blocker|major|nit, where, what, suggestion};
the three lists are merged and de-duplicated. What a reviewer could not read is
said in the document it gets, never guessed.

Who uses it:

    ask_claude      a brief of `spec_review.brief_chars` or more (tools/claude.py,
                    through `gate`): blockers go back to the agent once; the
                    fixed brief has its one re-review, then goes to Claude with
                    the remaining findings as accepted trade-offs. The findings
                    are noted on the brief's workstream.
    review_spec     the tool the research skill calls on a PRD or design before
                    ship_output: a review, and at most one re-review.

A review that cannot run (no reviewer answered) says so. It is never reported
as a review that found nothing.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from meeseek.config.paths import home

logger = logging.getLogger(__name__)

LENSES = ("comprehension", "zoom_out", "readiness")
SEVERITIES = ("blocker", "major", "nit")
DOC_CHARS = 40_000        # the document, as a reviewer reads it
MAX_REFS = 8              # files and links a review reads
REF_CHARS = 6_000         # characters one reference keeps
REFS_CHARS = 30_000       # all references together
PENDING_S = 3 * 3600      # a review waits this long for its re-review
TIMEOUT_S = 240

_URL = re.compile(r"https?://[^\s<>()\"'`\]]+")
_PATH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|md|ts|tsx|js|jsx|json|ya?ml|toml|go|sql|proto|sh|txt|"
                   r"cs|java|kt|swift|html|css))(?::\d+(?:-\d+)?)?(?![\w/])")


class Finding(BaseModel):
    severity: Literal["blocker", "major", "nit"] = Field(
        description="blocker: the work cannot start or would be built wrong; major: costly rework or confusion; "
                    "nit: polish")
    where: str = Field(default="", description="the section, heading or line of the document")
    what: str = Field(description="the problem, one or two sentences")
    suggestion: str = Field(default="", description="the change that removes it")


class Findings(BaseModel):
    findings: list[Finding] = Field(default_factory=list)


@dataclass
class Review:
    findings: list[dict] = field(default_factory=list)   # severity, where, what, suggestion, lenses
    ran: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    refs: list[str] = field(default_factory=list)        # what the reviewers read beside the document
    unread: list[str] = field(default_factory=list)      # references named but not readable

    @property
    def ok(self) -> bool:
        return bool(self.ran)

    @property
    def blockers(self) -> list[dict]:
        return [f for f in self.findings if f["severity"] == "blocker"]

    def counts(self) -> str:
        n = {s: sum(f["severity"] == s for f in self.findings) for s in SEVERITIES}
        return ", ".join(f"{n[s]} {s}" for s in SEVERITIES)

    def why_not(self) -> str:
        return "; ".join(f"{lens}: {err}" for lens, err in self.failed.items()) or "no reviewer ran"

    def render(self) -> str:
        """The findings as Markdown, blockers first."""
        if not self.ok:
            return f"The spec review did not run ({self.why_not()})."
        lines = [f"Spec review ({', '.join(self.ran)}): {self.counts()}."]
        if self.failed:
            lines.append(f"Did not run: {self.why_not()}. Its lens is not covered.")
        if self.unread:
            lines.append("Not readable, so not reviewed against: " + ", ".join(self.unread))
        for n, f in enumerate(self.findings, 1):
            where = f" ({f['where']})" if f["where"] else ""
            fix = f" → {f['suggestion']}" if f["suggestion"] else ""
            lines.append(f"{n}. [{f['severity']}]{where} {f['what']}{fix}")
        return "\n".join(lines)


def _model() -> str:
    from meeseek.config import cfg

    return cfg.spec_review.model or cfg.models.workflow


# --- what the document references ----------------------------------------------------

def references(document: str) -> list[str]:
    """The links and file paths the document names, in order, at most MAX_REFS."""
    found: list[str] = []
    for m in _URL.finditer(document):
        url = m.group(0).rstrip(".,;:!?")
        if url not in found:
            found.append(url)
    for m in _PATH.finditer(_URL.sub(" ", document)):
        if m.group(1) not in found and not m.group(1).startswith("."):
            found.append(m.group(1))
    return found[:MAX_REFS]


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _read_ref(ref: str, cwd: str) -> str:
    """A reference's text; raises when it cannot be read. Read only: a file in
    the brief's folder or the knowledge base, a page by fetch_url, a main-repo
    file (docs/ …) through the gitlab pack's get_file."""
    if ref.startswith(("http://", "https://")):
        from meeseek.tools.web import fetch_url

        text = fetch_url(ref)
        if text.startswith(("拒絕抓取", "抓取失敗", "讀取 Google 檔案失敗", f"（{ref} 沒有")):
            raise RuntimeError(text[:200])
        return text
    roots = [Path(cwd).expanduser()] if cwd else []
    roots.append(home().kb)
    for root in roots:
        rel = ref.removeprefix("knowledge_base/") if root == home().kb else ref
        p = root / rel
        if p.is_file() and _inside(root, p):
            return p.read_text(encoding="utf-8", errors="replace")
    from meeseek.config import cfg
    from meeseek.runtime import packs

    if not any(s.pack == "gitlab" for s in cfg.mcp_servers):
        raise RuntimeError("not in the folder or the knowledge base, and no gitlab pack to read the main repo")
    text = packs.call("get_file", {"file_path": ref, "num_lines": 400})
    if text.startswith("Error"):
        raise RuntimeError(text[:200])
    return text


def gather_refs(document: str, cwd: str = "") -> tuple[str, list[str], list[str]]:
    """(the references as one block, what was read, what could not be)."""
    parts, read, unread, room = [], [], [], REFS_CHARS
    for ref in references(document):
        if room <= 0:
            unread.append(f"{ref} (the review's reading budget is spent)")
            continue
        try:
            text = _read_ref(ref, cwd)
        except Exception as e:  # noqa: BLE001 — named in the review as unread
            unread.append(f"{ref} ({str(e)[:120]})")
            continue
        text = text[:min(REF_CHARS, room)]
        room -= len(text)
        read.append(ref)
        parts.append(f'<reference name="{ref}">\n{text}\n</reference>')
    if unread:
        parts.append("<unreadable>\n" + "\n".join(unread) + "\n</unreadable>")
    return "\n\n".join(parts), read, unread


# --- the review --------------------------------------------------------------------------

def _prompt(lens: str, document: str, refs: str) -> str:
    from meeseek.context import prompts

    refs = refs or "(none)"
    if lens == "comprehension":
        return prompts.text("internal/goldfish#comprehension", document=document, references=refs)
    if lens == "zoom_out":
        return prompts.text("internal/goldfish#zoom_out", document=document, references=refs)
    return prompts.text("internal/goldfish#readiness", document=document, references=refs)


async def _one(lens: str, prompt: str) -> list[dict]:
    from meeseek.runtime import oneshot

    got = await oneshot.aask(prompt, model=_model(), schema=Findings, temperature=0, timeout_s=TIMEOUT_S,
                             source="goldfish")
    return [{**f.model_dump(), "lenses": [lens]} for f in got.findings if f.what.strip()]


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", (text or "").lower()).strip()


def merge(lists: list[list[dict]]) -> list[dict]:
    """One list: a finding two reviewers both made (the same place, nearly the
    same words) kept once, at the higher severity; blockers first."""
    out: list[dict] = []
    for f in (f for fs in lists for f in fs):
        what, where = _norm(f["what"]), _norm(f["where"])
        twin = next((o for o in out
                     if (not where or not _norm(o["where"]) or where == _norm(o["where"]))
                     and difflib.SequenceMatcher(None, what, _norm(o["what"])).ratio() >= 0.75), None)
        if twin is None:
            out.append({**f, "lenses": list(f["lenses"])})
            continue
        if SEVERITIES.index(f["severity"]) < SEVERITIES.index(twin["severity"]):
            twin["severity"] = f["severity"]
        if len(f.get("suggestion") or "") > len(twin.get("suggestion") or ""):
            twin["suggestion"] = f["suggestion"]
        twin["lenses"] = sorted(set(twin["lenses"]) | set(f["lenses"]))
    return sorted(out, key=lambda f: SEVERITIES.index(f["severity"]))


async def areview(document: str, cwd: str = "") -> Review:
    """The three reviewers, in parallel, on `document` and what it references."""
    refs, read, unread = await asyncio.to_thread(gather_refs, document, cwd)
    doc = document if len(document) <= DOC_CHARS else document[:DOC_CHARS] + "\n…(cut)"
    results = await asyncio.gather(*(_one(lens, _prompt(lens, doc, refs)) for lens in LENSES),
                                   return_exceptions=True)
    review = Review(refs=read, unread=unread)
    lists = []
    for lens, got in zip(LENSES, results):
        if isinstance(got, BaseException):
            review.failed[lens] = str(got)[:200] or type(got).__name__
            logger.warning("[goldfish] %s did not run: %s", lens, got)
        else:
            review.ran.append(lens)
            lists.append(got)
    review.findings = merge(lists)
    return review


def review(document: str, cwd: str = "") -> Review:
    """`areview` from synchronous code."""
    from meeseek.engine import run_sync

    return run_sync(lambda: areview(document, cwd))


# --- one review and one re-review ----------------------------------------------------------

def _state_file() -> Path:
    return home().data / "goldfish.json"


def _pending() -> dict:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {k: v for k, v in data.items() if now - v.get("at", 0) < PENDING_S}


def _set_pending(key: str, n: int | None) -> None:
    data = _pending()
    if n is None:
        data.pop(key, None)
    else:
        data[key] = {"at": time.time(), "n": n}
    _state_file().parent.mkdir(parents=True, exist_ok=True)
    _state_file().write_text(json.dumps(data), encoding="utf-8")


def is_spec(brief: str) -> bool:
    from meeseek.config import cfg

    n = cfg.spec_review.brief_chars
    return bool(n) and len(brief or "") >= n


def trade_offs(review: Review) -> str:
    """The findings a brief goes with: known, and accepted for now."""
    lines = ["## Accepted trade-offs (spec review)",
             "A zero-context review of this brief found these. They are known and accepted for now: do not "
             "stop on them, but keep them in mind and say in your report if one turned out to matter."]
    for f in review.findings:
        where = f" ({f['where']})" if f["where"] else ""
        lines.append(f"- [{f['severity']}]{where} {f['what']}")
    return "\n".join(lines)


@dataclass
class Gate:
    send_back: str = ""        # non-empty: the brief does not go; this is the agent's answer
    brief: str = ""            # what goes to Claude
    note: str = ""             # a line for the agent's report


def gate(brief: str, *, cwd: str = "", key: str = "", workstream: str = "") -> Gate:
    """A spec-sized brief before it goes to Claude: blockers go back once, the
    re-review lets it go with what remains as accepted trade-offs."""
    key = f"brief|{key}|{cwd}"
    again = key in _pending()
    rev = review(brief, cwd)
    if not rev.ok:
        _set_pending(key, None)
        _note(workstream, f"Spec review did not run ({rev.why_not()}); the brief went to Claude unreviewed.")
        return Gate(brief=brief, note=f"(The spec review did not run: {rev.why_not()}. The brief went unreviewed.)")
    if rev.blockers and not again:
        _set_pending(key, 1)
        _note(workstream, f"Spec review of a Claude brief: {rev.counts()}; sent back to fix the blockers. "
                          + _heads(rev))
        return Gate(send_back=(
            f"Nothing went to Claude: the spec review found {len(rev.blockers)} blocker(s). Fix the blockers in "
            "the brief (the others too, where it is cheap) and call ask_claude again with the whole brief. It "
            "is reviewed once more, then it goes, with what is left attached as accepted trade-offs.\n\n"
            + rev.render()))
    _set_pending(key, None)
    which = "Re-review" if again else "Spec review"
    _note(workstream, f"{which} of the Claude brief: {rev.counts()}; sent with them as accepted trade-offs. "
                      + _heads(rev))
    out = f"{brief}\n\n{trade_offs(rev)}" if rev.findings else brief
    return Gate(brief=out, note=f"({which}: {rev.counts()}; the brief went with them as accepted trade-offs.)")


def _heads(rev: Review) -> str:
    return " | ".join(f"[{f['severity']}] {f['what'][:80]}" for f in rev.findings[:3])


def _note(ws_id: str, text: str) -> None:
    if not ws_id:
        return
    from meeseek.runtime import workstreams

    try:
        ws = workstreams.get(ws_id)
        if ws:
            workstreams.note(ws, text, "spec review")
    except Exception as e:  # noqa: BLE001 — the review stands without the note
        logger.warning("[goldfish] noting on %s: %s", ws_id, e)


# --- the tool ------------------------------------------------------------------------------

def _title(document: str) -> str:
    head = next((ln for ln in document.splitlines() if ln.strip()), "")
    return _norm(head)[:80]


def review_spec(content: str = "", path: str = "", _origin: dict | None = None) -> str:
    """The research skill's tool: a zero-context review of a PRD or design
    document before ship_output. One review and one re-review per document."""
    if not content and path:
        p = (home().kb / path.removeprefix("knowledge_base/")).resolve()
        if not _inside(home().kb, p) or not p.is_file():
            return f"Error: {path} is not a file in the knowledge base."
        content = p.read_text(encoding="utf-8", errors="replace")
    if not content.strip():
        return "Error: give the document's text (content) or its knowledge-base path (path)."
    key = f"tool|{(_origin or {}).get('session_id', '')}|{_title(content)}"
    n = _pending().get(key, {}).get("n", 0)
    if n >= 2:
        return ("This document had its review and its re-review. Do not review it again: ship it with the "
                "remaining findings in an 'Accepted trade-offs' section.")
    rev = review(content, str(home().kb))
    if not rev.ok:
        return f"The spec review did not run ({rev.why_not()}). Say so to the user; do not claim it was reviewed."
    _set_pending(key, n + 1)
    if n == 0 and rev.blockers:
        tail = ("Fix the blockers (and the majors where it is cheap), then call review_spec once more on the "
                "whole revised document. After that re-review, ship it; list what remains under 'Accepted "
                "trade-offs'.")
    elif n == 0:
        tail = ("No blockers. Fix what is cheap, list the rest under 'Accepted trade-offs', and ship it. No "
                "re-review is needed.")
    else:
        tail = "This was the re-review. Ship it now; list what remains under 'Accepted trade-offs'."
    return f"{rev.render()}\n\n{tail}"
