"""Quiet hours: Telegram's own pushes wait as wakes and go out once the hours
end — deferred, never dropped. Replies to the person's own messages don't wait."""

import asyncio
import time
from datetime import datetime

import pytest

from meeseek.runtime import inbox, messengers, wakes


class FakeTelegram:
    name = "telegram"

    def __init__(self):
        self.sent = []

    async def send(self, chat, texts):
        self.sent.append((chat, texts[0]))

    async def send_reply(self, chat, markdown, files):
        self.sent.append((chat, markdown))
        return []

    async def send_files(self, chat, paths):
        pass

    async def call(self, method, **kwargs):
        self.sent.append((method, kwargs.get("text", "")))


@pytest.fixture
def tg(tmp_path, monkeypatch):
    from meeseek.config import cfg
    from meeseek.config.schema import CoachMeeting

    monkeypatch.setattr(cfg.triggers, "coach_meetings", [CoachMeeting("Data team daily")])
    monkeypatch.setattr(cfg.triggers, "coach_telegram_chat", "42")
    fake = FakeTelegram()
    monkeypatch.setitem(messengers.channels(), "telegram", fake)
    return fake


def _at(hour, minute=0):
    return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


def _due_now():
    def fn(records):
        for r in records:
            r["due_at"] = 0.0
        return records
    wakes.wake_store().update(fn)


def _coach_item():
    it = inbox.file(source="calendar", key="calendar:daily", title="Data team daily", meta={})
    it.update(status="ready", reply="推演內容")
    inbox.save(it)
    return it


class TestQuietUntil:
    def test_overnight_window(self, monkeypatch):
        from meeseek.config import cfg

        monkeypatch.setattr(cfg.triggers, "quiet_hours", "23-8")
        assert messengers.quiet_until(_at(12)) == 0.0
        until = messengers.quiet_until(_at(23, 30))
        assert until > _at(23, 30) and datetime.fromtimestamp(until).hour == 8
        assert messengers.quiet_until(_at(7)) > 0.0   # the morning side of the window

    def test_daytime_window_and_off(self, monkeypatch):
        from meeseek.config import cfg

        monkeypatch.setattr(cfg.triggers, "quiet_hours", "1-6")
        assert messengers.quiet_until(_at(3)) > 0.0
        assert messengers.quiet_until(_at(12)) == 0.0
        monkeypatch.setattr(cfg.triggers, "quiet_hours", "")
        assert messengers.quiet_until(_at(3)) == 0.0
        monkeypatch.setattr(cfg.triggers, "quiet_hours", "garbage")
        assert messengers.quiet_until(_at(3)) == 0.0


class TestDeferredNotDropped:
    def test_a_coach_push_waits_and_goes_out_once(self, tg, monkeypatch):
        until = time.time() + 3600
        monkeypatch.setattr(messengers, "quiet_until", lambda now=None: until)
        it = _coach_item()
        asyncio.run(messengers.coach_push(it))
        asyncio.run(messengers.coach_push(inbox.get(it["id"])))   # held again: still one wake
        assert tg.sent == [] and not inbox.get(it["id"])["meta"].get("pushed")
        [w] = wakes.pending()
        assert w["kind"] == "quiet_push" and w["due_at"] == until

        monkeypatch.setattr(messengers, "quiet_until", lambda now=None: 0.0)   # the hours end
        _due_now()
        asyncio.run(wakes.poll())
        assert len(tg.sent) == 1 and tg.sent[0][1].startswith("**會前推演** · Data team daily")
        assert inbox.get(it["id"])["meta"]["pushed"]
        asyncio.run(wakes.poll())                                 # the wake is settled: sent once
        assert len(tg.sent) == 1 and wakes.pending() == []

    def test_proposals_wait_for_the_cron_after_the_hours(self, tg, monkeypatch):
        from meeseek.runtime import improve

        announced = []
        monkeypatch.setattr(improve, "unannounced",
                            lambda: [{"id": "p1", "skill": "s", "summary": "x", "kind": "lesson"}])
        monkeypatch.setattr(improve, "announced", lambda pid: announced.append(pid))
        monkeypatch.setattr(messengers, "quiet_until", lambda now=None: time.time() + 3600)
        assert asyncio.run(messengers.announce_proposals()) == 0
        assert tg.sent == [] and announced == []                  # kept, not claimed
        monkeypatch.setattr(messengers, "quiet_until", lambda now=None: 0.0)
        assert asyncio.run(messengers.announce_proposals()) == 1
        assert announced == ["p1"]

    def test_a_claude_result_waits_too(self, tg, monkeypatch):
        from meeseek.runtime import claude_agent

        record = {"id": "r1", "status": "done", "title": "修了排序",
                  "origin": {"channel": "telegram", "chat": "42"}}
        monkeypatch.setattr(claude_agent, "get", lambda rid: record if rid == "r1" else None)
        monkeypatch.setattr(claude_agent, "last_reply", lambda r: "改好了")
        monkeypatch.setattr(messengers, "quiet_until", lambda now=None: time.time() + 3600)
        asyncio.run(messengers.claude_done(record))
        assert tg.sent == [] and len(wakes.pending()) == 1

        monkeypatch.setattr(messengers, "quiet_until", lambda now=None: 0.0)
        _due_now()
        asyncio.run(wakes.poll())
        assert len(tg.sent) == 1 and "改好了" in tg.sent[0][1]

    def test_a_reply_to_the_person_never_waits(self, tg, monkeypatch):
        monkeypatch.setattr(messengers, "quiet_until",
                            lambda now=None: pytest.fail("deliver must not check the quiet hours"))

        async def no_session(item):
            return []
        monkeypatch.setattr(messengers, "_turn_texts", no_session)
        it = inbox.file(source="telegram", key="telegram:42:abc", title="問題", sender="You",
                        meta={"chat": "42"})
        it.update(status="ready", reply="答案")
        inbox.save(it)
        asyncio.run(messengers.deliver(it))
        assert tg.sent[0] == ("42", "答案")
