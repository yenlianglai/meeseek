"""Shared output manager for persisting agent-generated results."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from meeseek.config.paths import home


def save_session(output: str, *, category: str, topic: str) -> Path:
    """Save markdown to knowledge_base/outputs/{category}/{stamp}_{slug}.md.

    The stamp is to the second, so repeated saves of a fixed topic (a review,
    a daily plan) do not overwrite each other.
    """
    out_dir = home().outputs / category
    out_dir.mkdir(parents=True, exist_ok=True)

    slug = topic[:50].strip().replace(" ", "_").replace("/", "-")
    slug = "".join(c for c in slug if c.isalnum() or c in ("_", "-"))
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{date_str}_{slug}.md"

    filepath = out_dir / filename
    filepath.write_text(output, encoding="utf-8")
    return filepath
