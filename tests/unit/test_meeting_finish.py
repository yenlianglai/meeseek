"""A recorded meeting is transcribed once; notes and the coach's report share it."""

import asyncio
import time

import pytest

from meeseek.config import home

from meeseek.runtime import inbox, meeting_recorder
from meeseek.runtime.meeting_transcript import Line


@pytest.fixture
def item(tmp_path, monkeypatch):
    folder = tmp_path / "rec"
    folder.mkdir()
    it = inbox.file(source="meeting", key="meeting:x", title="Recording: Data team daily",
                    meta={"code": "dby-hvyt-odc", "dir": str(folder), "pid": 0, "started": time.time() - 900})
    monkeypatch.setattr(meeting_recorder, "mix", lambda f: f / "meeting.m4a")
    return it


def _patch(monkeypatch, lines, calls):
    import meeseek.context.meeting_notes as notes
    import meeseek.runtime.meeting_coach as coach
    import meeseek.runtime.meeting_transcript as mt

    def transcript(folder, **kw):
        calls.append("transcript")
        return lines
    monkeypatch.setattr(mt, "transcript", transcript)

    def make_notes(text, name, *, date, transcript_md=""):
        calls.append(("notes", text))
        p = home().root / "notes.md"
        p.write_text("# notes")
        return p
    monkeypatch.setattr(notes, "notes_from_transcript", make_notes)
    import meeseek.context.ontology as ontology

    monkeypatch.setattr(ontology, "learn", lambda *a, **kw: calls.append(("ontology", None)) or 0)
    monkeypatch.setattr(notes, "_load_team_context", lambda: "")
    monkeypatch.setattr(coach, "rehearsal_for", lambda *a: "")

    def report(folder, got, **kw):
        calls.append(("coach", got))
        (home().root / "report.md").write_text("# report")
        return {"report": "report.md", "transcript": "t.md"}
    monkeypatch.setattr(coach, "report", report)

    async def work(item_id, message=""):
        calls.append(("work", message))
    monkeypatch.setattr(inbox, "work", work)


def test_a_meeting_is_transcribed_once_for_notes_and_report(item, monkeypatch):
    from meeseek.config import cfg
    from meeseek.config.schema import CoachMeeting

    monkeypatch.setattr(cfg.triggers, "coach_meetings", [CoachMeeting("Data team daily")])
    lines = [Line("00:01", "我", "昨天送出 MR !168，今天修 !165 的 CR 意見。" * 10)]
    calls = []
    _patch(monkeypatch, lines, calls)
    asyncio.run(meeting_recorder._finish(item))
    assert calls.count("transcript") == 1
    assert [c[0] for c in calls if isinstance(c, tuple)] == ["notes", "ontology", "coach", "work"]
    assert next(c[1] for c in calls if isinstance(c, tuple) and c[0] == "coach") is lines
    assert inbox.get(item["id"])["meta"]["coach"]["report"] == "report.md"


def test_a_recording_with_almost_nothing_said_gets_no_notes(item, monkeypatch):
    calls = []
    _patch(monkeypatch, [Line("00:15", "我", "呃，今天要去哪裡玩呢？")], calls)
    asyncio.run(meeting_recorder._finish(item))
    assert calls == ["transcript"]
    after = inbox.get(item["id"])
    assert after["status"] == "ignored" and "沒有寫會議記錄" in after["reply"]


def test_a_restart_keeps_a_live_recording_going(tmp_path, monkeypatch):
    live = inbox.file(source="meeting", key="meeting:a", title="Recording: daily",
                      meta={"code": "aaa-bbbb-ccc", "dir": "a", "pid": 111, "started": time.time() - 60})
    dead = inbox.file(source="meeting", key="meeting:b", title="Recording: old",
                      meta={"code": "ddd-eeee-fff", "dir": "b", "pid": 222, "started": time.time() - 60})
    for it in (live, dead):
        it["status"] = "recording"
        inbox.save(it)
    stopped = []
    monkeypatch.setattr(meeting_recorder, "_alive", lambda pid: pid == 111)
    monkeypatch.setattr(meeting_recorder, "_stop", lambda pid: stopped.append(pid))
    meeting_recorder.recover()
    assert inbox.get(live["id"])["status"] == "recording"      # still being recorded
    assert inbox.get(dead["id"])["status"] == "recorded" and stopped == [222]


def test_old_recordings_are_pruned_but_not_one_in_use(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(meeting_recorder, "recordings_dir", lambda: tmp_path / "rec")
    old, busy, new = (tmp_path / "rec" / n for n in ("old", "busy", "new"))
    for d in (old, busy, new):
        d.mkdir(parents=True)
    month_ago = time.time() - 40 * 86400
    for d in (old, busy):
        os.utime(d, (month_ago, month_ago))
    it = inbox.file(source="meeting", key="meeting:busy", title="x", meta={"dir": str(busy)})
    it["status"] = "working"
    inbox.save(it)
    assert meeting_recorder.prune() == 1
    assert not old.exists() and busy.exists() and new.exists()


def test_the_calendar_fallback_skips_declined_meetings(monkeypatch):
    from meeseek.runtime import packs

    events = [{"code": "aaa-bbbb-ccc", "title": "a", "start": "x", "declined": False},
              {"code": "ddd-eeee-fff", "title": "b", "start": "x", "declined": True}]

    asked = []
    monkeypatch.setattr(packs, "call_json", lambda name, args=None: asked.append(name) or {"items": events})
    assert meeting_recorder.calendar_codes(now=1_000_000) == ["aaa-bbbb-ccc"]
    assert asked == ["meetings"], "the calendar is read through its pack"


_SAID = [Line("00:01", "我", "我這週盤點閒置服務，Casey 更新登入流程。" * 10)]


def test_the_meetings_jira_work_is_held_before_its_turn(item, monkeypatch):
    from meeseek.config import cfg
    from meeseek.runtime import meeting_tickets

    monkeypatch.setattr(cfg.triggers, "meeting_tickets", {"project": "ACME"})
    calls = []
    _patch(monkeypatch, _SAID, calls)

    async def propose(it, notes, **kw):
        calls.append(("propose", kw["title"]))
        inbox.hold(inbox.get(it["id"]), "jira_add_comment", {"issue_key": "ACME-7", "body": "b"}, "meeting_tickets")
        return [{"owner": "Casey", "whose": "team", "what": "更新登入流程", "due": "", "proposal": "comment",
                 "ticket": "ACME-7", "action_id": "x"}]
    monkeypatch.setattr(meeting_tickets, "propose", propose)
    asyncio.run(meeting_recorder._finish(item))
    assert [c[0] for c in calls if isinstance(c, tuple)] == ["notes", "ontology", "propose", "work"]
    message = next(c[1] for c in calls if isinstance(c, tuple) and c[0] == "work")
    assert "提出在 ACME-7 留言" in message and "不要再為這些待辦提出 Jira 寫入" in message
    assert [a["tool"] for a in inbox.get(item["id"])["actions"]] == ["jira_add_comment"]


def test_without_jira_proposals_the_turn_searches_before_it_proposes(item, monkeypatch):
    calls = []
    _patch(monkeypatch, _SAID, calls)
    asyncio.run(meeting_recorder._finish(item))
    message = next(c[1] for c in calls if isinstance(c, tuple) and c[0] == "work")
    assert "先用 jira_search 找既有的票" in message


def test_a_failed_proposal_step_keeps_the_notes(item, monkeypatch):
    from meeseek.config import cfg
    from meeseek.runtime import meeting_tickets

    monkeypatch.setattr(cfg.triggers, "meeting_tickets", {"project": "ACME"})
    calls = []
    _patch(monkeypatch, _SAID, calls)

    async def broken(*a, **kw):
        raise RuntimeError("model down")
    monkeypatch.setattr(meeting_tickets, "propose", broken)
    asyncio.run(meeting_recorder._finish(item))
    after = inbox.get(item["id"])
    assert after["meta"]["tickets_error"] and after["body"] == "# notes"
    assert "先用 jira_search" in next(c[1] for c in calls if isinstance(c, tuple) and c[0] == "work")
