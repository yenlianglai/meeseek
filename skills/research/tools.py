"""The research skill's own tools: the knowledge base's write path and Drive sync."""

import re
from pathlib import Path
from datetime import date

from meeseek.config.paths import home
from meeseek.context.sync import sync


def refinement_dir() -> Path:
    return home().kb / "notes"


def sources_drive_dir() -> Path:
    return home().sources / "drive"


def save_refinement(topic: str, content: str) -> str:
    """Save new knowledge or clarifications to the knowledge base.

    Args:
        topic: Short topic name in English with underscores (e.g. "deployment_flow")
        content: The structured knowledge to save (Markdown format)
    """
    refinement_dir().mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    slug = re.sub(r"^\d{4}-\d{2}-\d{2}_?", "", topic.strip()).replace(" ", "_")[:40]
    filename = f"{today}_{slug}.md"
    filepath = refinement_dir() / filename
    filepath.write_text(
        f"# {topic}\n\n_Updated: {today}_\n\n{content}",
        encoding="utf-8",
    )
    return f"Saved to knowledge_base/notes/{filename}"


def update_kb_file(filename: str, section: str, new_content: str) -> str:
    """Update or append a section in an existing knowledge base file.

    Args:
        filename: A synced Drive document in knowledge_base/sources/drive/ (e.g. "design_doc.md")
        section: The section heading to update. If not found, appends.
        new_content: The corrected/updated content for that section
    """
    filepath = sources_drive_dir() / filename
    if not filepath.exists():
        return f"Error: {filename} not found in knowledge_base/sources/drive/"

    existing = filepath.read_text(encoding="utf-8")
    today = date.today().isoformat()
    correction_note = f"\n\n> _[Updated {today}]_ {new_content}"

    if section in existing:
        insert_pos = existing.index(section) + len(section)
        next_section = existing.find("\n## ", insert_pos)
        if next_section == -1:
            updated = existing + correction_note
        else:
            updated = existing[:next_section] + correction_note + "\n" + existing[next_section:]
    else:
        updated = existing + f"\n\n{section}\n{correction_note}"

    filepath.write_text(updated, encoding="utf-8")
    return f"Updated {filename} — added correction under '{section}'"


TOOLS = [save_refinement, update_kb_file, sync]
