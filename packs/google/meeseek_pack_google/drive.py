"""Drive and Calendar reads for meeseek's own code (internal tools): a file
as Markdown for fetch_url; meetings, in terms that are not Google's (a code,
a recording), for the meeting recorder and the cloud recordings; a
recording's metadata and bytes for the meeting notes."""

from __future__ import annotations

from pathlib import Path

from meeseek_pack_google.auth import get_credentials
from meeseek_pack_google.gdrive import build_service, convert_file_to_md, list_folder_files
from meeseek_pack_google.settings import settings

_SLIDES_MIME = "application/vnd.google-apps.presentation"
LOOKUP_TIMEOUT_S = 30


def drive_read(file_id: str) -> dict:
    """A Drive file as Markdown: {name, mime, markdown}, or {error} a person can act on."""
    from googleapiclient.errors import HttpError

    creds = get_credentials()
    drive = build_service("drive", "v3", creds)
    try:
        meta = drive.files().get(fileId=file_id, fields="id,name,mimeType,modifiedTime",
                                 supportsAllDrives=True).execute()
        if meta["mimeType"] == _SLIDES_MIME:
            text = drive.files().export(fileId=file_id, mimeType="text/plain").execute()
            md = f"# {meta['name']}\n\n{text.decode('utf-8') if isinstance(text, bytes) else text}"
        else:
            md = convert_file_to_md(drive, build_service("sheets", "v4", creds), meta,
                                    docs_service=build_service("docs", "v1", creds))
    except HttpError as e:
        status = getattr(e.resp, "status", "?")
        if status in (403, 404):
            return {"error": f"讀不到這個 Google 檔案（HTTP {status}）：使用者的 Google 帳號沒有權限，或檔案不存在。"
                             "請檔案擁有者把它分享給使用者，再試一次。"}
        return {"error": f"讀取 Google 檔案失敗（HTTP {status}）：{str(e)[:300]}"}
    if md is None:
        return {"error": f"「{meta['name']}」是 {meta['mimeType']}，不是能轉成文字的檔案。"}
    return {"name": meta["name"], "mime": meta["mimeType"], "markdown": md}


def recording_info(recording_id: str) -> dict:
    """A recording's {id, name, modified, owned_by_me}."""
    drive = build_service("drive", "v3", get_credentials(), timeout=LOOKUP_TIMEOUT_S)
    f = drive.files().get(fileId=recording_id, fields="id,name,modifiedTime,ownedByMe",
                          supportsAllDrives=True).execute()
    return {"id": f.get("id", recording_id), "name": f.get("name", ""), "modified": f.get("modifiedTime", ""),
            "owned_by_me": bool(f.get("ownedByMe"))}


def recording_fetch(recording_id: str, dest: str) -> dict:
    """A recording's bytes, written to `dest` (a path meeseek names): {path, bytes}."""
    file_id = recording_id
    from googleapiclient.http import MediaIoBaseDownload

    drive = build_service("drive", "v3", get_credentials())
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        dl = MediaIoBaseDownload(f, req, chunksize=16 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return {"path": str(path), "bytes": path.stat().st_size}


def meeting_recordings() -> dict:
    """The files in the configured meetings folder: {files: [{id, name, mimeType, modifiedTime}]}."""
    folder_id = settings().meetings_folder_id
    if not folder_id:
        return {"error": "No meetings folder configured (mcp_servers.google.config.meetings_folder_id in project.yaml)"}
    drive = build_service("drive", "v3", get_credentials())
    return {"files": list_folder_files(drive, folder_id)}


def meetings(time_min: str, time_max: str, max_results: int = 250) -> dict:
    """Your calendar's meetings between two RFC 3339 times, in start order:
    {items: [{id, title, start, end, code, link, me, declined, attendees,
    recording}]} — `code` the Meet meeting code ("" without a Meet link),
    `recording` {id, name} once Meet attached one, else null."""
    svc = build_service("calendar", "v3", get_credentials(), timeout=LOOKUP_TIMEOUT_S)
    events = svc.events().list(calendarId="primary", timeMin=time_min, timeMax=time_max, singleEvents=True,
                               orderBy="startTime", maxResults=int(max_results)).execute().get("items", [])
    return {"items": [_meeting(ev) for ev in events]}


def _meeting(ev: dict) -> dict:
    link = ev.get("hangoutLink") or ""
    people = ev.get("attendees") or []
    me = next((f"{a.get('displayName', '')} {a.get('email', '')}".strip() for a in people if a.get("self")), "")
    rec = next((a for a in ev.get("attachments") or []
                if (a.get("mimeType") or "").startswith("video/") and a.get("fileId")), None)
    return {"id": ev.get("id", ""), "title": ev.get("summary", ""),
            "start": (ev.get("start") or {}).get("dateTime", ""), "end": (ev.get("end") or {}).get("dateTime", ""),
            "code": link.rsplit("/", 1)[-1] if "meet.google.com/" in link else "", "link": link, "me": me,
            "declined": any(a.get("self") and a.get("responseStatus") == "declined" for a in people),
            "attendees": [a.get("displayName") or a.get("email", "") for a in people if not a.get("self")],
            "recording": {"id": rec["fileId"], "name": rec.get("title", "")} if rec else None}
