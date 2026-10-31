"""Gmail API adapter (read-only) — the user's own mailbox via ADC.

Uses the GA Gmail API; the Gmail MCP server requires Developer Preview
enrollment. Output is compact text sized for the agent loop (bodies
truncated; get_gmail_thread reads a full conversation).
"""

from __future__ import annotations

import base64


from meeseek_pack_google.auth import get_credentials
from meeseek_pack_google.gdrive import build_service

_MAX_BODY_CHARS = 2_000


def _svc():
    return build_service("gmail", "v1", get_credentials())


def _extract_body(payload: dict) -> str:
    """Best-effort plain-text body from a message payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        data = payload["body"]["data"]
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text:
            return text
    return ""


def _headers(msg: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def search_gmail(query: str, max_results: int = 10) -> str:
    """Search the user's Gmail with standard Gmail query syntax.

    Args:
        query: Gmail search query, e.g. 'from:someone newer_than:7d', 'subject:"每週問題"'
        max_results: Max threads to return (default 10, capped at 20)
    """
    max_results = min(int(max_results or 10), 20)
    svc = _svc()
    resp = svc.users().threads().list(userId="me", q=query, maxResults=max_results).execute()
    threads = resp.get("threads", [])
    if not threads:
        return f"No Gmail threads match: {query}"

    lines = [f"Found {len(threads)} thread(s) for '{query}':\n"]
    for t in threads:
        detail = svc.users().threads().get(
            userId="me", id=t["id"], format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        ).execute()
        msgs = detail.get("messages", [])
        h = _headers(msgs[0]) if msgs else {}
        lines.append(
            f"- thread_id={t['id']} ({len(msgs)} msgs) | {h.get('date', '?')[:22]} | "
            f"{h.get('from', '?')[:40]} | {h.get('subject', '(no subject)')[:80]}"
        )
    lines.append("\nUse get_gmail_thread(thread_id) to read a conversation.")
    return "\n".join(lines)


def get_gmail_thread(thread_id: str) -> str:
    """Read a full Gmail thread (all messages, bodies truncated).

    Args:
        thread_id: Thread id from search_gmail results
    """
    detail = _svc().users().threads().get(userId="me", id=thread_id, format="full").execute()
    msgs = detail.get("messages", [])
    if not msgs:
        return f"Thread {thread_id} has no messages."

    sections = []
    for m in msgs:
        h = _headers(m)
        body = _extract_body(m.get("payload", {})).strip() or "(no plain-text body)"
        if len(body) > _MAX_BODY_CHARS:
            body = body[:_MAX_BODY_CHARS] + "\n... (truncated)"
        sections.append(
            f"### {h.get('date', '?')[:22]} | {h.get('from', '?')}\n"
            f"Subject: {h.get('subject', '')}\n\n{body}"
        )
    return f"Thread {thread_id} — {len(msgs)} message(s)\n\n" + "\n\n---\n\n".join(sections)
