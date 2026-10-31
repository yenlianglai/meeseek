"""The meeting coach: what it measures, what it remembers, what it finds."""

import time

from meeseek.runtime import meeting_coach
from meeseek.runtime.meeting_coach import Line


def test_the_numbers_count_only_your_lines():
    lines = [Line("00:01", "我", "嗯那個，就是我昨天把 ACME-1 做完了"),
             Line("00:10", "與會者", "就是就是就是"),
             Line("00:20", "我", "今天做 ACME-2")]
    n = meeting_coach.numbers(lines)
    assert n["你發言的段數"] == 2
    assert n["你說的字數"] == len(lines[0].text) + len(lines[2].text)
    assert n["口頭禪"] == {"嗯": 1, "那個": 1, "就是": 1}   # 與會者's 就是 are not yours


def test_the_report_watches_the_named_people(monkeypatch):
    """向他人學習 opens by naming who the person wants to learn from, when they
    have said (triggers.coach_learn_from); without names it still looks."""
    from meeseek.config import cfg

    monkeypatch.setattr(cfg.triggers, "coach_learn_from", ["Casey", "Sasha"])
    assert "Casey、Sasha" in meeting_coach.learn_note()
    monkeypatch.setattr(cfg.triggers, "coach_learn_from", [])
    assert meeting_coach.learn_note() == ""


def test_only_the_named_meetings_are_coached(monkeypatch):
    from meeseek.config import cfg

    from meeseek.config.schema import CoachMeeting

    monkeypatch.setattr(cfg.triggers, "coach_meetings", [
        CoachMeeting("Data team daily"), CoachMeeting("Acme App 例會"),
        CoachMeeting("一起看Acme App例會問題", focus="V5 文件")])
    assert meeting_coach.coached("Data team daily")
    assert meeting_coach.coached("Acme App  例會").focus == ""     # the calendar's double space
    assert not meeting_coach.coached("Data team ( 一)")          # Monday's 10:00 is not the daily
    assert not meeting_coach.coached("Acme App會議室")             # a room booking
    assert meeting_coach.coached("一起看Acme App例會問題").focus == "V5 文件"
    monkeypatch.setattr(cfg.triggers, "coach_meetings", [])
    assert not meeting_coach.coached("Data team daily")


def test_the_next_rehearsal_gets_what_the_last_report_said_to_practise(tmp_path, monkeypatch):
    monkeypatch.setattr(meeting_coach, "coach_dir", lambda: tmp_path)
    for name, practice in [("2026-10-05-1100-data-team-daily", "先講結論"),
                           ("2026-10-09-1100-data-team-daily", "請求要具體"),
                           ("2026-10-10-1400-prd-review", "少說就是")]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "report.md").write_text(f"# x\n\n## 下次練習\n{practice}\n\n## 其他\nno\n")
    # this meeting's own newest report wins over a newer report of another meeting
    assert "請求要具體" in meeting_coach.practice_note("Data team daily")
    assert "少說就是" in meeting_coach.practice_note("All hands")      # none of its own: the newest
    monkeypatch.setattr(meeting_coach, "coach_dir", lambda: tmp_path / "none")
    assert meeting_coach.practice_note("Data team daily") == ""


def test_the_report_finds_its_rehearsal_by_meet_code_or_title(monkeypatch):
    now = time.time()
    items = [
        {"source": "calendar", "title": "Data team daily", "reply": "old", "received_at": now - 86400,
         "meta": {"meet": "https://meet.google.com/aaa-bbbb-ccc"}},
        {"source": "calendar", "title": "Data team daily", "reply": "today", "received_at": now - 1800,
         "meta": {"meet": "https://meet.google.com/aaa-bbbb-ccc"}},
        {"source": "calendar", "title": "Other", "reply": "x", "received_at": now - 1800, "meta": {"meet": ""}},
    ]
    from meeseek.runtime import inbox

    monkeypatch.setattr(inbox, "items", lambda limit=100: items)
    assert meeting_coach.rehearsal_for("aaa-bbbb-ccc", "Data team daily", now) == "today"
    assert meeting_coach.rehearsal_for("", "Data team daily", now) == "today"
    assert meeting_coach.rehearsal_for("zzz-zzzz-zzz", "Nothing", now) == ""


def test_timestamps_are_shifted_into_the_whole_meeting():
    from meeseek.runtime import meeting_transcript

    assert meeting_transcript._shift("01:05", 600) == "11:05"
    assert meeting_transcript._shift("garbled", 600) == "10:00"
    assert meeting_coach.slug(0, "Data team daily!").endswith("data-team-daily")


def test_an_inbox_turn_that_ended_on_a_model_error_is_recognised():
    from meeseek.context import prompts
    from meeseek.runtime import inbox

    notice = prompts.text("runtime/callbacks#model_error", detail="ClientError: 400 INVALID_ARGUMENT")
    assert inbox._model_failed(notice)
    assert not inbox._model_failed("## 這場會要達成什麼\n…")


def test_coach_meetings_take_a_name_or_a_name_and_focus():
    from meeseek.config.loader import _coach_meetings

    ms = _coach_meetings(["Data team daily", {"name": "一起看Acme App例會問題", "focus": "V5"}, {"focus": "no name"}])
    assert [(m.name, m.focus) for m in ms] == [("Data team daily", ""), ("一起看Acme App例會問題", "V5")]


def test_a_coached_meetings_audio_goes_after_90_days_its_report_stays():
    import os
    import time

    from meeseek.runtime import meeting_coach

    old = meeting_coach.coach_dir() / "2026-07-01-standup"
    new = meeting_coach.coach_dir() / "2026-10-20-standup"
    for d in (old, new):
        d.mkdir(parents=True)
        (d / "recording.m4a").write_bytes(b"x")
        (d / "report.md").write_text("r")
    long_ago = time.time() - 91 * 86400
    os.utime(old / "recording.m4a", (long_ago, long_ago))
    assert meeting_coach.prune_audio() == 1
    assert not (old / "recording.m4a").exists() and (old / "report.md").exists()
    assert (new / "recording.m4a").exists()
