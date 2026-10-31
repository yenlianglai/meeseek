"""Gemini looks at a web page an agent built — a plugin (review_page).

The agent writes the page; it cannot see it rendered. This renders it at
phone and desktop width (page_shot) and asks the multimodal Gemini model what
a person would find broken or unreadable — cut-off content, overlap, an empty
chart, labels that do not fit — and hands the list back to the agent that
asked, which fixes what it decides to. Stateless: a path in, a list out.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from meeseek.context import prompts
from meeseek.runtime import page_shot

logger = logging.getLogger(__name__)

# Tall windows, the empty band below cut by page_shot: the whole page is in
# the picture, so nothing below a fold reads as missing.
PHONE = (500, 8000)
DESKTOP = (1280, 6000)
BLOCKING = ("high", "medium")   # what is sent back to fix; low is only noted


class Issue(BaseModel):
    severity: str = Field(description="high / medium / low")
    view: str = Field(description="phone / desktop / both")
    where: str = Field(description="哪個位置")
    problem: str = Field(description="問題是什麼")
    fix: str = Field(description="怎麼修，一句話")


class Review(BaseModel):
    ok: bool
    summary: str
    issues: list[Issue] = Field(default_factory=list)


def review(page: Path, purpose: str) -> dict | None:
    """What Gemini sees wrong with `page`; None when it could not be looked at
    (no Chrome, no model) — a review that did not happen blocks nothing."""
    from meeseek.config import cfg
    from meeseek.runtime.oneshot import ask

    phone = page_shot.screenshot(page, *PHONE)
    desktop = page_shot.screenshot(page, *DESKTOP)
    if phone is None or desktop is None:
        return None
    model = cfg.claude.page_review_model or cfg.models.multimodal
    try:
        r = ask(prompts.text("internal/page_review#review", purpose=purpose or "（沒有說明）"),
                model=model, schema=Review, temperature=0,
                files=[("image/png", phone.read_bytes()), ("image/png", desktop.read_bytes())])
    except Exception as e:  # noqa: BLE001 — the page still stands; it just was not checked
        logger.warning("[page_review] %s: %s", page.name, e)
        return None
    issues = [i.model_dump() for i in r.issues]
    return {"page": str(page), "ok": r.ok and not any(i["severity"] in BLOCKING for i in issues),
            "summary": r.summary, "issues": issues, "model": model,
            "shots": {"phone": str(phone), "desktop": str(desktop)}}


def review_page(path: str, purpose: str = "") -> str:
    """The plugin: what is wrong with the page at `path` (under the outputs
    directory), as text for the agent that built it."""
    from meeseek.config.paths import home

    root = home().outputs.resolve()
    page = Path(path).expanduser()
    page = (page if page.is_absolute() else root / page).resolve()
    try:
        page.relative_to(root)
    except ValueError:
        return f"review_page looks only at pages under {root}; {path} is not."
    if not page.is_file() or page.suffix.lower() not in (".html", ".htm"):
        return f"No HTML page at {page}."
    r = review(page, purpose)
    if r is None:
        return "The page could not be looked at (no Chrome or the vision model failed); nothing is known about it."
    if not r["issues"]:
        return f"OK — {r['summary']}"
    lines = [f"- [{i['severity']}·{i['view']}] {i['where']}: {i['problem']} — fix: {i['fix']}" for i in r["issues"]]
    head = "Fix the high and medium ones; low is polish." if not r["ok"] else "Nothing blocking; low is polish."
    return f"{r['summary']}\n{head}\n" + "\n".join(lines)
