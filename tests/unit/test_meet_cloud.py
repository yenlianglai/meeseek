"""Meet's own recording, for a coached meeting the laptop did not record."""

import time
from datetime import datetime, timezone

from meeseek.runtime import inbox, meet_cloud


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _event(start, end, title="Data team daily", rec=True, eid="e1"):
    """A meeting as a calendar pack's `meetings` answers it."""
    return {"id": eid, "title": title, "code": "dby-hvyt-odc", "link": "https://meet.example/dby-hvyt-odc",
            "start": _iso(start), "end": _iso(end), "me": "", "declined": False, "attendees": [],
            "recording": {"id": "F1", "name": "Recording"} if rec else None}


def _due(events, now, mine=False):
    return meet_cloud.due(events, now, file=lambda fid: {"owned_by_me": mine})


def test_only_coached_ended_unrecorded_meetings_with_a_recording_are_due(monkeypatch):
    from meeseek.config import cfg
    from meeseek.config.schema import CoachMeeting

    monkeypatch.setattr(cfg.triggers, "coach_meetings", [CoachMeeting("Data team daily")])
    now = time.time()
    items = []
    monkeypatch.setattr(inbox, "items", lambda limit=100: items)
    monkeypatch.setattr(inbox, "by_key", lambda key: None)
    ended = (now - 3600, now - 2700)
    assert [e["id"] for e in _due([_event(*ended)], now)] == ["e1"]
    assert _due([_event(*ended, title="All hands")], now) == []        # not coached, not yours
    assert [e["id"] for e in _due([_event(*ended, title="All hands")], now, mine=True)] == ["e1"]   # yours
    assert _due([_event(*ended, rec=False)], now) == []                # no recording yet
    assert _due([_event(now - 600, now - 60)], now) == []              # just ended: may be recording
    assert _due([_event(now - 90000, now - 89000)], now) == []         # over a day ago
    # recorded and coached on the laptop: nothing to fetch
    items.append({"source": "meeting", "status": "ready",
                  "meta": {"code": "dby-hvyt-odc", "started": ended[0] + 30, "coach": {"report": "x"}}})
    assert _due([_event(*ended)], now) == []
    # the laptop's recording failed: fetch the cloud one
    items[0] = {"source": "meeting", "status": "failed", "meta": {"code": "dby-hvyt-odc", "started": ended[0] + 30}}
    assert [e["id"] for e in _due([_event(*ended)], now)] == ["e1"]
    # the laptop's copy had almost nothing in it — maybe the laptop's fault: fetch
    items[0] = {"source": "meeting", "status": "ignored", "meta": {"code": "dby-hvyt-odc", "started": ended[0] + 30}}
    assert [e["id"] for e in _due([_event(*ended)], now)] == ["e1"]


def test_poll_keeps_its_google_calls_off_the_event_loop(monkeypatch):
    """`due` looks recordings up on Drive; run on the loop, a slow network
    stalled the whole server — every MCP call, the console, the bots — for
    minutes (2026-10-14). The calendar list and every lookup run in a thread."""
    import asyncio
    import threading

    from meeseek.runtime import packs

    on_loop = []

    def watch(*a, **k):
        on_loop.append(threading.current_thread() is threading.main_thread())
        return []

    monkeypatch.setattr(packs, "call_json", lambda name, args=None: {"items": watch()})
    monkeypatch.setattr(meet_cloud, "due", lambda events, now: watch())
    assert asyncio.run(meet_cloud.poll()) == 0
    assert on_loop and not any(on_loop)


def test_a_recording_not_shared_with_you_does_not_stop_the_others(monkeypatch):
    """Meet attaches a recording you may not open: Drive answers 404 for it."""
    monkeypatch.setattr(inbox, "by_key", lambda key: None)
    now = time.time()
    ended = (now - 3600, now - 2700)

    def file(fid):
        if fid == "F1":
            raise RuntimeError("HttpError 404 File not found")
        return {"owned_by_me": True}

    theirs = _event(*ended, title="All hands")
    mine = {**_event(*ended, title="Design review", eid="e2"), "recording": {"id": "F2", "name": "Recording"}}
    assert [e["id"] for e in meet_cloud.due([theirs, mine], now, file=file)] == ["e2"]
