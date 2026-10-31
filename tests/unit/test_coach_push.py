"""A coached meeting's rehearsal and report reach your phone — that chat only, once."""

import asyncio

import pytest

from meeseek.runtime import inbox, messengers


class FakeTelegram:
    name = "telegram"

    def __init__(self):
        self.sent = []

    async def send_reply(self, chat, markdown, files):
        self.sent.append((chat, markdown, [p.name for p in files]))
        return []

    async def send_files(self, chat, paths):
        self.sent.append((chat, "files", [p.name for p in paths]))


@pytest.fixture
def tg(tmp_path, monkeypatch):
    from meeseek.config import cfg
    from meeseek.config.schema import CoachMeeting

    monkeypatch.setattr(cfg.triggers, "coach_meetings", [CoachMeeting("Data team daily")])
    monkeypatch.setattr(cfg.triggers, "coach_telegram_chat", "42")
    fake = FakeTelegram()
    monkeypatch.setitem(messengers.channels(), "telegram", fake)
    return fake


def _item(source, title, reply="內容", status="ready", meta=None):
    it = inbox.file(source=source, key=f"{source}:{title}", title=title, meta=meta or {})
    it.update(status=status, reply=reply)
    inbox.save(it)
    return it


def test_a_coached_rehearsal_goes_once_to_that_chat(tg):
    it = _item("calendar", "Data team daily")
    asyncio.run(messengers.coach_push(it))
    asyncio.run(messengers.coach_push(inbox.get(it["id"])))
    assert len(tg.sent) == 1 and tg.sent[0][0] == "42" and tg.sent[0][1].startswith("**會前推演** · Data team daily")


def test_other_items_stay_in_the_inbox(tg):
    for it in (_item("calendar", "All hands"), _item("gmail", "Data team daily"),
               _item("calendar", "Data team daily", status="failed"), _item("meeting", "Recording: Data team daily")):
        asyncio.run(messengers.coach_push(it))
    assert tg.sent == []      # not coached; not a meeting item; failed; a meeting with no report


def test_a_report_goes_with_its_files(tg, monkeypatch, tmp_path):
    from meeseek.config import home

    for f in ("report.md", "transcript.md", "recording.m4a"):
        (home().root / f).write_text("x")
    it = _item("meeting", "Recording: Data team daily", reply="**你的表現** …",
               meta={"coach": {"report": "report.md", "transcript": "transcript.md", "recording": "recording.m4a"}})
    asyncio.run(messengers.coach_push(it))
    assert tg.sent[0][1].startswith("**會後報告** · Data team daily")
    assert tg.sent[0][2] == ["report.md", "transcript.md", "recording.m4a"]
    assert tg.sent[1] == ("42", "files", ["report.md", "transcript.md", "recording.m4a"])


def test_no_chat_named_no_push(tg, monkeypatch):
    from meeseek.config import cfg

    monkeypatch.setattr(cfg.triggers, "coach_telegram_chat", "")
    asyncio.run(messengers.coach_push(_item("calendar", "Data team daily")))
    assert tg.sent == []
