"""meeseek's Google Workspace pack: Gmail, Chat, Drive and Calendar, read-only,
as an MCP server (`python -m meeseek_pack_google`).

    agents read     search_gmail, get_gmail_thread, list_chat_spaces, get_chat_messages
    sources         gmail_poll, chat_poll, calendar_poll (meeseek's trigger loop)
    internal        drive_read, drive_sync, meeting_recordings, meetings,
                    recording_info, recording_fetch, auth_status — for
                    meeseek's own code: fetch_url on a Google link, the
                    knowledge base's Drive mirror, the meeting notes

Credentials: the machine's Application Default Credentials with the
Workspace scopes (`python -m meeseek_pack_google.auth --check`).
"""

from __future__ import annotations

from meeseek_pack_kit import Tool, params


def _auth_status() -> dict:
    from meeseek_pack_google import auth

    return auth.check_workspace_scopes()


_POLL = {"since": {"type": "number"}, "state": {"type": "object"}}


def tools() -> list[Tool]:
    # Here, not at import: `python -m meeseek_pack_google.auth` loads this
    # package first, and must not find its own module imported already.
    from meeseek_pack_google import drive, drive_sync, sources
    from meeseek_pack_google.gchat import get_chat_messages, list_chat_spaces
    from meeseek_pack_google.gmail import get_gmail_thread, search_gmail

    return [
        # --- Gmail（唯讀，使用者本人信箱） ---
        Tool(
            name="search_gmail",
            description="Search the user's Gmail with standard Gmail query syntax (e.g. 'from:x newer_than:7d', 'subject:\"每週問題\"')",
            fn=search_gmail,
            parameters=params({
                "query": {"type": "string", "description": "Gmail search query"},
                "max_results": {"type": "integer", "description": "Max threads (default 10, cap 20)"},
            }, ["query"]),
        ),
        Tool(
            name="get_gmail_thread",
            description="Read a full Gmail thread (all messages) by thread_id from search_gmail",
            fn=get_gmail_thread,
            parameters=params({"thread_id": {"type": "string", "description": "Thread id"}}, ["thread_id"]),
        ),
        # --- Google Chat（唯讀，使用者所屬 spaces/DM） ---
        Tool(
            name="list_chat_spaces",
            description="List Google Chat spaces/DMs the user belongs to",
            fn=list_chat_spaces,
            parameters=params({"max_results": {"type": "integer", "description": "Max spaces (default 30)"}}),
        ),
        Tool(
            name="get_chat_messages",
            description="Read recent messages from a Chat space (newest first)",
            fn=get_chat_messages,
            parameters=params({
                "space_name": {"type": "string", "description": "Space resource name, e.g. 'spaces/AAAA1234'"},
                "max_results": {"type": "integer", "description": "Max messages (default 25)"},
            }, ["space_name"]),
        ),
        # --- sources: what arrived on its own ---
        Tool("gmail_poll", "New mail matching `query` (a trigger source).", sources.gmail_poll,
             params({**_POLL, "query": {"type": "string"}}), source=True),
        Tool("chat_poll", "Direct messages and @mentions (a trigger source).", sources.chat_poll,
             params(_POLL), source=True),
        Tool("calendar_poll", "Meetings starting within `lead_minutes` (a trigger source).", sources.calendar_poll,
             params({**_POLL, "lead_minutes": {"type": "integer"}}), source=True),
        # --- internal: meeseek's own code calls these by name ---
        Tool("drive_read", "A Drive file as Markdown: {name, mime, markdown} or {error}.", drive.drive_read,
             params({"file_id": {"type": "string"}}, ["file_id"]), internal=True),
        Tool("recording_info", "A recording's {id, name, modified, owned_by_me}.", drive.recording_info,
             params({"recording_id": {"type": "string"}}, ["recording_id"]), internal=True),
        Tool("recording_fetch", "A recording's bytes, written to `dest`.", drive.recording_fetch,
             params({"recording_id": {"type": "string"}, "dest": {"type": "string"}}, ["recording_id", "dest"]),
             side_effect=True, internal=True),
        Tool("drive_sync", "Mirror the configured Drive folders as Markdown into `dest`.",
             lambda dest, history_file: drive_sync.sync(dest, history_file),
             params({"dest": {"type": "string"}, "history_file": {"type": "string"}}, ["dest", "history_file"]),
             side_effect=True, internal=True),
        Tool("meeting_recordings", "The files in the configured meetings folder.", drive.meeting_recordings,
             params({}), internal=True),
        Tool("meetings", "Your calendar's meetings between two times, with their recordings.", drive.meetings,
             params({"time_min": {"type": "string"}, "time_max": {"type": "string"},
                     "max_results": {"type": "integer"}}, ["time_min", "time_max"]), internal=True),
        Tool("auth_status", "Whether the login has the Workspace scopes.", _auth_status, params({}), internal=True),
    ]
