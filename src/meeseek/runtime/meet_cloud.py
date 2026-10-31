"""The meeting service's own recordings of meetings the laptop did not record.

A calendar pack answers which meetings ended with a recording attached
(`meetings`: id, title, times, the meeting's code, a recording's id), whose a
recording is (`recording_info`) and its bytes (`recording_fetch`) — the Google
pack, for Meet: Meet records to the organizer's Drive when the meeting's space
is set to record automatically (Calendar → event → video call settings →
Meeting records → Record automatically; once per recurring meeting). Nothing
here writes to the calendar or the recordings.

Every few minutes `poll()` looks at meetings that ended in the last day and
carry a recording — your own (in your Drive) and the coached ones
(`triggers.coach_meetings`). One the laptop (runtime/meeting_recorder.py)
recorded and finished is left alone; any other becomes an Inbox item: the
video is downloaded, its sound taken as the meeting's one track, and the item
is finished like a local recording. Recordings appear late, so a meeting is
looked for until a day has passed.

Each recording settled here — fetched, or left to the laptop's copy — goes
into the backfill's history (.cache/meeting_sync_history.json), so a manual `/meetings`
(context/meeting_notes.py) does not make the same notes twice.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path

from meeseek.runtime import inbox

logger = logging.getLogger(__name__)

POLL_S = 300
LOOK_BACK_S = 24 * 3600
SETTLE_S = 10 * 60          # a meeting this recently ended may still be recording locally
_NEAR_S = 45 * 60           # a local recording of the same meeting started within this of it


# Fetches in flight, held so they are not collected mid-way.
_fetching: set[asyncio.Task] = set()


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _done_locally(code: str, start: float, coached: bool) -> bool:
    """The laptop recorded this meeting and finished it — notes, and a coach
    report when it is coached — or is still on its way there."""
    for item in inbox.items():
        if item["source"] != "meeting" or item["meta"].get("cloud"):
            continue
        meta = item["meta"]
        if meta.get("code") != code or abs(meta.get("started", 0) - start) > _NEAR_S:
            continue
        if item["status"] in ("recording", "paused", "recorded", "working"):
            return True
        # A copy with almost nothing in it may be the laptop's failure, not the
        # meeting's: Meet's recording is fetched then.
        if meta.get("coach") if coached else meta.get("notes"):
            return True
    return False


_files: dict[str, dict] = {}


def _file(file_id: str) -> dict:
    """A recording's {id, name, modified, owned_by_me}, looked up once."""
    if file_id not in _files:
        from meeseek.runtime import packs

        _files[file_id] = packs.call_json("recording_info", {"recording_id": file_id})
    return _files[file_id]


def settle(file_id: str, how: str) -> None:
    """Put a recording in the backfill's history: it needs no notes from there."""
    from meeseek.context.meeting_notes import _load_history, _save_history

    history = _load_history()
    if file_id in history:
        return
    f = _file(file_id)
    history[file_id] = {"name": f.get("name", ""), "modifiedTime": f.get("modified", ""),
                        "processed_at": datetime.now().isoformat(), "by": how}
    _save_history(history)


def due(events: list[dict], now: float, file=_file) -> list[dict]:
    """The meetings (the pack's `meetings`) to fetch a cloud recording for now.
    A recording the laptop's copy makes unnecessary is settled on the way."""
    from meeseek.runtime.meeting_coach import coached

    out = []
    for ev in events:
        start, end, code, rec = ev.get("start"), ev.get("end"), ev.get("code"), ev.get("recording")
        if not (start and end and code and rec and now - LOOK_BACK_S < _ts(end) < now - SETTLE_S):
            continue
        if inbox.by_key(f"cloud:{ev['id']}"):
            continue
        is_coached = coached(ev.get("title", "")) is not None
        if not is_coached:
            try:
                mine = file(rec["id"]).get("owned_by_me")
            except Exception as e:  # noqa: BLE001 — not shared with you (Drive's 404): not yours either
                logger.info("[meet_cloud] %s: its recording cannot be looked up: %s", ev.get("title", ""), e)
                continue
            if not mine:
                continue
        if _done_locally(code, _ts(start), is_coached):
            if file is _file:
                settle(rec["id"], "local")
            continue
        out.append(ev)
    return out


def _download(file_id: str, dest: Path) -> Path:
    from meeseek.runtime import packs

    packs.call_json("recording_fetch", {"recording_id": file_id, "dest": str(dest)})
    return dest


def _sound(video: Path) -> Path:
    """The video's sound, as the meeting's one track (app.m4a beside it)."""
    out = video.with_name("app.m4a")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "aac", "-b:a", "48k", str(out)], check=True, capture_output=True, timeout=1800)
    video.unlink(missing_ok=True)
    return out


async def _fetch_and_finish(item: dict, file_id: str) -> None:
    from meeseek.runtime import meeting_recorder

    folder = Path(item["meta"]["dir"])
    try:
        await asyncio.to_thread(settle, file_id, "cloud")
        video = await asyncio.to_thread(_download, file_id, folder / "cloud.mp4")
        await asyncio.to_thread(_sound, video)
    except Exception as e:  # noqa: BLE001
        logger.exception("[meet_cloud] fetching %s failed", item["id"])
        item = inbox.get(item["id"]) or item
        missing = "File not found" in str(e) or "404" in str(e)
        item["status"], item["error"] = "failed", (
            "Meet's recording of this meeting is not shared with you (Drive answers 404 for it: it sits in the "
            "organizer's Drive). Ask them to share it, or have the laptop record the meeting." if missing
            else f"Could not fetch Meet's recording: {type(e).__name__}: {e}")
        inbox.save(item)
        from meeseek.runtime import messengers

        await messengers.meeting_failed(item)
        return
    meeting_recorder._finish_later(inbox.get(item["id"]) or item)


async def poll() -> int:
    """File and start an item for each meeting whose recording to fetch; how many."""
    from meeseek.runtime import packs
    from meeseek.runtime.meeting_recorder import recordings_dir
    from meeseek.runtime.triggers import _iso

    now = time.time()

    def to_fetch() -> list[dict]:
        events = packs.call_json("meetings", {"time_min": _iso(now - LOOK_BACK_S - 4 * 3600),
                                              "time_max": _iso(now), "max_results": 250})["items"]
        return due(events, now)

    filed = 0
    # Off the event loop, all of it: `due` looks each recording up on Drive and
    # settles some, and a slow network held those calls on the loop for minutes
    # (2026-10-14: every MCP call, the console and the bots stalled 13:11–13:19).
    for ev in await asyncio.to_thread(to_fetch):
        start, end, code = _ts(ev["start"]), _ts(ev["end"]), ev["code"]
        folder = recordings_dir() / f"{datetime.fromtimestamp(start):%Y%m%d-%H%M}-{code}-cloud"
        item = inbox.file(source="meeting", key=f"cloud:{ev['id']}", title=f"Recording: {ev.get('title', '')}",
                          link=ev.get("link", ""),
                          meta={"code": code, "dir": str(folder), "pid": 0, "started": start, "end": end,
                                "me": ev.get("me", ""), "cloud": True})
        if item is None:
            continue
        item["status"] = "working"
        inbox.save(item)
        task = asyncio.get_running_loop().create_task(_fetch_and_finish(item, ev["recording"]["id"]))
        _fetching.add(task)
        task.add_done_callback(_fetching.discard)
        filed += 1
    return filed
