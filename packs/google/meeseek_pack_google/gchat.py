"""Google Chat API adapter (read-only) — the user's spaces/DMs via ADC.

Uses the GA Chat API; the Chat MCP server requires Developer Preview enrollment.
"""

from __future__ import annotations


from meeseek_pack_google.auth import get_credentials
from meeseek_pack_google.gdrive import build_service

_MAX_TEXT_CHARS = 500


def _svc():
    return build_service("chat", "v1", get_credentials())


def list_chat_spaces(max_results: int = 30) -> str:
    """List Google Chat spaces/DMs the user is a member of.

    Args:
        max_results: Max spaces to return (default 30, capped at 100)
    """
    max_results = min(int(max_results or 30), 100)
    resp = _svc().spaces().list(pageSize=max_results).execute()
    spaces = resp.get("spaces", [])
    if not spaces:
        return "No Chat spaces found."

    lines = [f"Found {len(spaces)} space(s):\n"]
    for sp in spaces:
        kind = sp.get("spaceType", sp.get("type", "?"))
        lines.append(f"- {sp['name']} | {kind} | {sp.get('displayName') or '(DM)'}")
    lines.append("\nUse get_chat_messages(space_name) to read a space.")
    return "\n".join(lines)


def get_chat_messages(space_name: str, max_results: int = 25) -> str:
    """Read recent messages from a Chat space (newest first).

    Args:
        space_name: Space resource name from list_chat_spaces, e.g. "spaces/AAAA1234"
        max_results: Max messages (default 25, capped at 100)
    """
    max_results = min(int(max_results or 25), 100)
    resp = _svc().spaces().messages().list(
        parent=space_name, pageSize=max_results, orderBy="createTime desc",
    ).execute()
    msgs = resp.get("messages", [])
    if not msgs:
        return f"No messages in {space_name}."

    lines = [f"{space_name} — latest {len(msgs)} message(s), newest first:\n"]
    for m in msgs:
        sender = (m.get("sender") or {}).get("name", "?").split("/")[-1]
        text = (m.get("text") or "(non-text message)").replace("\n", " ")
        if len(text) > _MAX_TEXT_CHARS:
            text = text[:_MAX_TEXT_CHARS] + "…"
        lines.append(f"- [{m.get('createTime', '?')[:16]}] {sender}: {text}")
    return "\n".join(lines)
