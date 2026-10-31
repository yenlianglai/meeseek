"""Meeting recording: a Meet tab open starts the recorder, the tab closing
stops it and hands the recording on; a restart never orphans a recorder."""

import asyncio
import shutil
import subprocess
import time

import pytest

from meeseek.runtime import inbox
from meeseek.runtime import meeting_recorder as mr


@pytest.fixture(autouse=True)
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "_failed", {})
    monkeypatch.setattr(mr, "_ended", {})
    monkeypatch.setattr(mr, "in_call", lambda code: None)   # no Chrome in tests


def _recording(code="abc-defg-hij", **meta):
    item = inbox.file(source="meeting", key=f"meeting:{code}", title="會議錄音：週會",
                      meta={"code": code, "dir": "/tmp/x", "pid": 4242, "started": time.time(), "end": 0, **meta})
    item["status"] = "recording"
    inbox.save(item)
    return item


class TestTick:
    def test_an_open_meet_tab_starts_a_recording(self, monkeypatch):
        started = []
        monkeypatch.setattr(mr, "open_meetings", lambda: ["abc-defg-hij"])
        monkeypatch.setattr(mr, "_start", started.append)
        asyncio.run(mr.tick())
        assert started == ["abc-defg-hij"]

    def test_one_tab_list_without_the_meeting_does_not_stop_it(self, monkeypatch):
        """2026-10-20: a planning meeting cut into 34 recordings, a tick at a time."""
        item = _recording()
        monkeypatch.setattr(mr, "open_meetings", lambda: [])
        monkeypatch.setattr(mr, "_alive", lambda pid: True)
        monkeypatch.setattr(mr, "_stop", lambda pid: pytest.fail("one missing tab list is not a closed tab"))
        asyncio.run(mr.tick())
        assert inbox.get(item["id"])["meta"]["gone_since"]
        monkeypatch.setattr(mr, "open_meetings", lambda: ["abc-defg-hij"])   # back: forgotten
        asyncio.run(mr.tick())
        assert "gone_since" not in inbox.get(item["id"])["meta"]

    def test_a_missing_tab_keeps_recording_while_the_calendar_meeting_is_on(self, monkeypatch):
        """2026-10-22: a daily cut in two while the person never left — a
        minimized Chrome window drops out of the AppleScript window list, so
        absence is no evidence while the calendar says the meeting is on."""
        item = _recording(end=time.time() + 1800)
        monkeypatch.setattr(mr, "open_meetings", lambda: [])
        monkeypatch.setattr(mr, "_alive", lambda pid: True)
        monkeypatch.setattr(mr, "_stop", lambda pid: pytest.fail("the calendar says the meeting is on"))
        for _ in range(3):   # unseen for many ticks, well past GONE_GRACE_S
            asyncio.run(mr.tick())
        got = inbox.get(item["id"])
        assert got["status"] == "recording" and got["meta"]["tab_unseen"]
        assert "gone_since" not in got["meta"]
        monkeypatch.setattr(mr, "open_meetings", lambda: ["abc-defg-hij"])   # back: forgotten
        asyncio.run(mr.tick())
        assert "tab_unseen" not in inbox.get(item["id"])["meta"]

    def test_a_dead_recorder_resumes_even_while_the_tab_is_unseen(self, monkeypatch):
        item = _recording(end=time.time() + 1800)
        resumed = []
        monkeypatch.setattr(mr, "open_meetings", lambda: [])
        monkeypatch.setattr(mr, "_alive", lambda pid: False)
        monkeypatch.setattr(mr, "_resume", lambda i: resumed.append(i["id"]))
        asyncio.run(mr.tick())
        assert resumed == [item["id"]]

    def test_a_closed_tab_pauses_the_meeting_in_case_it_comes_back(self, monkeypatch):
        item = _recording(gone_since=time.time() - mr.GONE_GRACE_S - 1)
        stopped = []
        monkeypatch.setattr(mr, "open_meetings", lambda: [])
        monkeypatch.setattr(mr, "_alive", lambda pid: True)
        monkeypatch.setattr(mr, "_stop", stopped.append)
        monkeypatch.setattr(mr, "_finish_later", lambda i: pytest.fail("not finished yet: it may come back"))
        asyncio.run(mr.tick())
        got = inbox.get(item["id"])
        assert stopped == [4242] and got["status"] == "paused" and got["meta"]["stopped"] == "its tab closed"

    def test_a_meeting_back_soon_goes_on_in_the_same_item(self, monkeypatch):
        item = _recording()
        item["status"], item["meta"]["paused_at"] = "paused", time.time() - 60
        inbox.save(item)
        resumed = []
        monkeypatch.setattr(mr, "open_meetings", lambda: ["abc-defg-hij"])
        monkeypatch.setattr(mr, "_resume", lambda i: resumed.append(i["id"]))
        monkeypatch.setattr(mr, "_start", lambda code: pytest.fail("a new item for the same meeting"))
        asyncio.run(mr.tick())
        assert resumed == [item["id"]]

    def test_a_meeting_away_for_long_is_finished(self, monkeypatch):
        item = _recording()
        item["status"], item["meta"]["paused_at"] = "paused", time.time() - mr.RESUME_S - 1
        inbox.save(item)
        finished = []
        monkeypatch.setattr(mr, "open_meetings", lambda: [])
        monkeypatch.setattr(mr, "_finish_later", lambda i: finished.append(i["id"]))
        asyncio.run(mr.tick())
        assert finished == [item["id"]]

    def test_a_recorder_that_stopped_on_its_own_goes_on_in_a_new_part(self, monkeypatch):
        item = _recording()
        resumed = []
        monkeypatch.setattr(mr, "open_meetings", lambda: ["abc-defg-hij"])
        monkeypatch.setattr(mr, "_alive", lambda pid: False)
        monkeypatch.setattr(mr, "_resume", lambda i: resumed.append(i["id"]))
        monkeypatch.setattr(mr, "_finish_later", lambda i: pytest.fail("the meeting is still on"))
        asyncio.run(mr.tick())
        assert resumed == [item["id"]]

    def test_a_meeting_ended_past_its_end_is_not_started_again_from_its_tab(self, monkeypatch):
        """2026-10-13: started and stopped every minute for 53 minutes."""
        _recording(end=time.time() - mr.MAX_AFTER_END_S - 60)
        monkeypatch.setattr(mr, "open_meetings", lambda: ["abc-defg-hij"])
        monkeypatch.setattr(mr, "_alive", lambda pid: True)
        monkeypatch.setattr(mr, "_stop", lambda pid: None)
        monkeypatch.setattr(mr, "_finish_later", lambda i: (i.update(status="working"), inbox.save(i)))
        monkeypatch.setattr(mr, "_start", lambda code: pytest.fail("the tab is on 'you left the meeting'"))
        asyncio.run(mr.tick())
        asyncio.run(mr.tick())
        started = []
        monkeypatch.setattr(mr, "in_call", lambda code: True)   # rejoined in the same tab
        monkeypatch.setattr(mr, "_start", started.append)
        asyncio.run(mr.tick())
        assert started == ["abc-defg-hij"]

    def test_a_new_recording_past_the_end_is_asked_before_it_is_stopped(self, monkeypatch):
        """The probe runs before the decision: a recording never stops on a
        call state nobody has read yet."""
        item = _recording(end=time.time() - mr.MAX_AFTER_END_S - 60)
        monkeypatch.setattr(mr, "open_meetings", lambda: ["abc-defg-hij"])
        monkeypatch.setattr(mr, "_alive", lambda pid: True)
        monkeypatch.setattr(mr, "in_call", lambda code: True)
        monkeypatch.setattr(mr, "_stop", lambda pid: pytest.fail("the meeting is running over"))
        asyncio.run(mr.tick())
        assert inbox.get(item["id"])["meta"]["in_call"] is True

    def test_still_open_keeps_recording(self, monkeypatch):
        _recording()
        monkeypatch.setattr(mr, "open_meetings", lambda: ["abc-defg-hij"])
        monkeypatch.setattr(mr, "_alive", lambda pid: True)
        monkeypatch.setattr(mr, "_stop", lambda pid: pytest.fail("must not stop"))
        monkeypatch.setattr(mr, "_start", lambda code: pytest.fail("one recording at a time"))
        asyncio.run(mr.tick())

    def test_the_calendar_end_stops_a_tab_left_open(self, monkeypatch):
        _recording(end=time.time() - mr.MAX_AFTER_END_S - 60)
        stopped = []
        monkeypatch.setattr(mr, "open_meetings", lambda: ["abc-defg-hij"])
        monkeypatch.setattr(mr, "_alive", lambda pid: True)
        monkeypatch.setattr(mr, "_stop", stopped.append)

        monkeypatch.setattr(mr, "_finish_later", lambda i: None)
        asyncio.run(mr.tick())
        assert stopped == [4242]

    def test_a_meeting_running_over_is_still_recorded(self, monkeypatch):
        """Past the calendar's end, the tab still in the call keeps it going."""
        _recording(end=time.time() - mr.MAX_AFTER_END_S - 60, in_call=True)
        monkeypatch.setattr(mr, "open_meetings", lambda: ["abc-defg-hij"])
        monkeypatch.setattr(mr, "_alive", lambda pid: True)
        monkeypatch.setattr(mr, "_stop", lambda pid: pytest.fail("the meeting is still on"))
        monkeypatch.setattr(mr, "_watch_call", lambda item: asyncio.sleep(0))
        asyncio.run(mr.tick())

    @pytest.mark.parametrize("in_call", [False, None])
    def test_past_the_end_a_tab_not_seen_in_the_call_stops(self, monkeypatch, in_call):
        """Left on Meet's "you left" page (False), or the tab could not say (None)."""
        _recording(end=time.time() - mr.MAX_AFTER_END_S - 60, in_call=in_call)
        stopped = []
        monkeypatch.setattr(mr, "open_meetings", lambda: ["abc-defg-hij"])
        monkeypatch.setattr(mr, "_alive", lambda pid: True)
        monkeypatch.setattr(mr, "_stop", stopped.append)
        monkeypatch.setattr(mr, "_finish_later", lambda i: None)
        asyncio.run(mr.tick())
        assert stopped == [4242]

    def test_the_time_cap_stops_even_a_meeting_in_the_call(self, monkeypatch):
        _recording(started=time.time() - mr.MAX_RECORDING_S - 60, in_call=True)
        stopped = []
        monkeypatch.setattr(mr, "open_meetings", lambda: ["abc-defg-hij"])
        monkeypatch.setattr(mr, "_alive", lambda pid: True)
        monkeypatch.setattr(mr, "_stop", stopped.append)
        monkeypatch.setattr(mr, "_finish_later", lambda i: None)
        asyncio.run(mr.tick())
        assert stopped == [4242]

    def test_the_tab_says_whether_it_is_in_the_call(self, monkeypatch):
        item = _recording()
        monkeypatch.setattr(mr, "in_call", lambda code: True)
        asyncio.run(mr._watch_call(item))
        assert inbox.get(item["id"])["meta"]["in_call"] is True

    def test_a_meeting_that_failed_to_record_is_not_retried_at_once(self, monkeypatch):
        mr._failed["abc-defg-hij"] = time.time()
        monkeypatch.setattr(mr, "open_meetings", lambda: ["abc-defg-hij"])
        monkeypatch.setattr(mr, "_start", lambda code: pytest.fail("retried too soon"))
        asyncio.run(mr.tick())


class TestRestart:
    def test_a_recording_is_stopped_and_queued_for_transcription(self, monkeypatch):
        item = _recording()
        stopped = []
        monkeypatch.setattr(mr, "_stop", stopped.append)
        mr.recover()
        assert stopped == [4242] and inbox.get(item["id"])["status"] == "recorded"

    def test_no_pid_never_signals_a_process_group(self, monkeypatch):
        monkeypatch.setattr(mr.os, "kill", lambda *a: pytest.fail("signalled pid <= 0"))
        mr._stop(0)
        assert not mr._alive(0)


class TestWindowTitleFallback:
    """A full-screen Chrome window is missing from AppleScript's tab list; its
    System Events title, matched to an in-progress calendar meeting, still
    counts as open (2026-10-23: a meeting joined full screen was never recorded)."""

    @pytest.fixture(autouse=True)
    def _fresh_cache(self, monkeypatch):
        monkeypatch.setattr(mr, "_title_cal", (0.0, []))

    def test_a_fullscreen_meet_window_counts_as_open(self, monkeypatch):
        monkeypatch.setattr(mr, "meet_codes", lambda: [])
        monkeypatch.setattr(mr, "_window_names", lambda: [
            "Meet - Acme App 記憶模組討論 - 麥克風正在錄音 - Google Chrome - (王小明) (example.com)",
            "meeseek console"])
        monkeypatch.setattr(mr, "_calendar_meetings", lambda now=None: [("xzz-jowo-wdn", "Acme App 記憶模組討論")])
        assert mr.open_meetings() == ["xzz-jowo-wdn"]

    def test_a_title_matching_no_event_adds_nothing(self, monkeypatch):
        monkeypatch.setattr(mr, "meet_codes", lambda: ["abc-defg-hij"])
        monkeypatch.setattr(mr, "_window_names", lambda: ["Meet - 別場會議 - Google Chrome"])
        monkeypatch.setattr(mr, "_calendar_meetings", lambda now=None: [("xzz-jowo-wdn", "Acme App 記憶模組討論")])
        assert mr.open_meetings() == ["abc-defg-hij"]

    def test_no_meet_window_asks_no_calendar(self, monkeypatch):
        monkeypatch.setattr(mr, "meet_codes", lambda: [])
        monkeypatch.setattr(mr, "_window_names", lambda: ["GitHub - Google Chrome"])
        monkeypatch.setattr(mr, "_calendar_meetings",
                            lambda now=None: pytest.fail("no Meet window: the calendar is not asked"))
        assert mr.open_meetings() == []

    def test_unreadable_titles_change_nothing(self, monkeypatch):
        monkeypatch.setattr(mr, "meet_codes", lambda: ["abc-defg-hij"])

        def boom():
            raise RuntimeError("osascript is not allowed assistive access")

        monkeypatch.setattr(mr, "_window_names", boom)
        assert mr.open_meetings() == ["abc-defg-hij"]

    def test_a_meet_window_on_no_calendar_still_records_by_its_title(self, monkeypatch):
        """A meeting joined from a pasted link — on nobody's calendar — gets a
        stable key from its window title, so attending is enough to record."""
        monkeypatch.setattr(mr, "meet_codes", lambda: [])
        monkeypatch.setattr(mr, "_window_names", lambda: [
            "Meet - 臨時討論 Acme App - 麥克風正在錄音 - Google Chrome - (王小明)"])
        monkeypatch.setattr(mr, "_calendar_meetings", lambda now=None: [])
        first = mr.open_meetings()
        assert first == mr.open_meetings() and len(first) == 1 and first[0].startswith("ttl-")
        assert mr._title_names[first[0]] == "臨時討論 Acme App"

    def test_a_visible_tab_suppresses_the_title_key(self, monkeypatch):
        """With a real code on screen the same meeting must not double under a
        title key (one window, one tab, no way to pair them)."""
        monkeypatch.setattr(mr, "meet_codes", lambda: ["abc-defg-hij"])
        monkeypatch.setattr(mr, "_window_names", lambda: ["Meet - 臨時討論 - Google Chrome"])
        monkeypatch.setattr(mr, "_calendar_meetings", lambda now=None: [])
        assert mr.open_meetings() == ["abc-defg-hij"]

    def test_the_window_name_is_cut_before_the_mic_state(self):
        assert mr._meet_window_name("Meet - 週會 - 麥克風正在錄音 - Google Chrome - (x)") == "週會"
        assert mr._meet_window_name("Meet - Standup - Microphone recording - Google Chrome") == "Standup"
        assert mr._meet_window_name("Meet - 週會 - Google Chrome") == "週會"
        assert mr._meet_window_name("GitHub - Google Chrome") == ""

    def test_a_title_keyed_recording_is_named_by_the_title(self, monkeypatch):
        monkeypatch.setattr(mr, "_calendar_event", lambda code: {})
        monkeypatch.setattr(mr, "_spawn", lambda folder: (None, "no recorder in tests"))
        code = mr._title_code("臨時討論")
        mr._title_names[code] = "臨時討論"
        mr._start(code)
        item = next(i for i in inbox.items() if i["meta"].get("code") == code)
        assert item["title"] == "Recording: 臨時討論" and item["link"] == ""

    def test_the_calendar_answer_is_kept_a_minute(self, monkeypatch):
        calls = []
        monkeypatch.setattr(mr, "meet_codes", lambda: [])
        monkeypatch.setattr(mr, "_window_names", lambda: ["Meet - 週會 - Google Chrome"])

        def cal(now=None):
            calls.append(1)
            return [("abc-defg-hij", "週會")]

        monkeypatch.setattr(mr, "_calendar_meetings", cal)
        assert mr.open_meetings() == ["abc-defg-hij"]
        assert mr.open_meetings() == ["abc-defg-hij"]
        assert len(calls) == 1


class TestUnreadableMeetings:
    def test_a_recording_goes_on_when_meetings_cannot_be_read(self, monkeypatch):
        _recording()

        def unreadable():
            raise RuntimeError("tabs and calendar both unreadable")

        monkeypatch.setattr(mr, "open_meetings", unreadable)
        monkeypatch.setattr(mr, "_alive", lambda pid: True)
        monkeypatch.setattr(mr, "_stop", lambda pid: pytest.fail("must not stop on a read failure"))
        asyncio.run(mr.tick())

    def test_the_time_limit_still_stops_it(self, monkeypatch):
        _recording(started=time.time() - mr.MAX_RECORDING_S - 1)
        stopped = []

        def unreadable():
            raise RuntimeError("unreadable")

        monkeypatch.setattr(mr, "open_meetings", unreadable)
        monkeypatch.setattr(mr, "_alive", lambda pid: True)
        monkeypatch.setattr(mr, "_stop", stopped.append)
        monkeypatch.setattr(mr, "_finish_later", lambda i: None)
        asyncio.run(mr.tick())
        assert stopped == [4242]


def test_finishing_runs_beside_the_tick(monkeypatch):
    item = _recording()
    item["status"] = "recorded"
    inbox.save(item)
    started = []

    async def slow(i):
        started.append(i["id"])
        await asyncio.sleep(3600)

    monkeypatch.setattr(mr, "_finish", slow)
    monkeypatch.setattr(mr, "open_meetings", lambda: [])

    async def run():
        await asyncio.wait_for(mr.tick(), 2)  # returns at once, though the finish never ends
        await asyncio.sleep(0)
        assert inbox.get(item["id"])["status"] == "working" and started == [item["id"]]
        for t in list(mr._finishing):
            t.cancel()

    asyncio.run(run())


def test_a_permission_failure_says_what_to_allow():
    assert mr._explain("Code=-3801 使用者拒絕應用程式、視窗、顯示器擷取的TCC") == mr._PERMISSION_HINT


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_two_tracks_mix_into_one(tmp_path):
    for name, freq in (("app", 440), ("mic", 660)):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"sine=frequency={freq}:duration=1", "-c:a", "aac", str(tmp_path / f"{name}.m4a")],
                       check=True)
    out = mr.mix(tmp_path)
    assert out.name == "meeting.m4a" and out.stat().st_size > 0


def test_no_audio_is_an_error(tmp_path):
    with pytest.raises(RuntimeError):
        mr.mix(tmp_path)


def test_automation_denied_says_what_to_allow(monkeypatch):
    class Out:
        returncode, stdout = 1, ""
        stderr = "87:90: execution error: 未獲授權來傳送Apple Event到Google Chrome。 (-1743)"

    monkeypatch.setattr(mr.subprocess, "run", lambda *a, **k: Out())
    with pytest.raises(RuntimeError) as e:
        mr.meet_codes()
    assert "Automation" in str(e.value) and "tccutil" in str(e.value)


def test_the_calendar_stands_in_for_unreadable_tabs(monkeypatch):
    def denied():
        raise RuntimeError(mr._AUTOMATION_HINT)

    monkeypatch.setattr(mr, "meet_codes", denied)
    monkeypatch.setattr(mr, "calendar_codes", lambda: ["abc-defg-hij"])
    assert mr.open_meetings() == ["abc-defg-hij"] and mr.fallback_reason == mr._AUTOMATION_HINT


def test_a_tab_that_cannot_be_asked_says_why_on_the_item(monkeypatch):
    item = _recording(in_call=True)

    def denied(code):
        raise RuntimeError(mr._JS_HINT)

    monkeypatch.setattr(mr, "in_call", denied)
    asyncio.run(mr._watch_call(item))
    meta = inbox.get(item["id"])["meta"]
    assert meta["in_call"] is None and meta["call_error"] == mr._JS_HINT


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_a_meeting_recorded_in_parts_is_joined_once(tmp_path):
    def tone(path, seconds):
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"sine=frequency=440:duration={seconds}", "-c:a", "aac", str(path)], check=True)

    for track in ("app.m4a", "mic.m4a"):
        tone(tmp_path / track, 2)
        tone(tmp_path / "part2" / track, 3)
    parts = [{"dir": "part2", "started": 0}]
    mr.join_parts(tmp_path, parts)
    mr.join_parts(tmp_path, parts)   # a retried finish joins the same parts, not the joined file again
    from meeseek.runtime.meeting_transcript import duration
    assert 4.8 < duration(tmp_path / "mic.m4a") < 5.3
    assert (tmp_path / "part1" / "mic.m4a").exists()


def test_a_restart_mid_transcription_transcribes_it_again():
    item = _recording()
    item["status"] = "working"
    inbox.save(item)
    noted = _recording(code="xyz-abcd-efg")
    noted["status"], noted["meta"]["notes"] = "working", "kb/n.md"   # in its agent turn: the Inbox retries that
    inbox.save(noted)
    mr.recover()
    assert inbox.get(item["id"])["status"] == "recorded"
    assert inbox.get(noted["id"])["status"] == "working"


def test_a_meet_tab_is_found_whatever_path_meet_puts_before_the_code():
    """2026-10-21: a daily joined at meet.google.com/meet/<code> was never recorded."""
    tabs = ("https://meet.google.com/meet/dby-hvyt-odc, https://meet.google.com/kix-jvjg-kqw?authuser=0, "
            "https://meet.google.com/landing, https://meet.google.com/abc-defg-hijk")
    assert sorted(set(mr._MEET.findall(tabs))) == ["dby-hvyt-odc", "kix-jvjg-kqw"]
