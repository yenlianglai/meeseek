"""The Inbox digest: what waits unseen goes to the phone on a cron — once per
state, held by the quiet hours, and 全部標為已讀 clears it from there."""

import asyncio
import time

import pytest

from meeseek.runtime import inbox, messengers, wakes


class FakeTelegram:
    name = "telegram"

    def __init__(self):
        self.sent = []

    def allowed(self, user):
        return True

    async def call(self, method, **kwargs):
        self.sent.append((method, kwargs))


@pytest.fixture
def tg(tmp_path, monkeypatch):
    from meeseek.config import cfg

    monkeypatch.setattr(cfg.triggers, "coach_telegram_chat", "42")
    monkeypatch.setattr(cfg.triggers, "quiet_hours", "")
    fake = FakeTelegram()
    monkeypatch.setitem(messengers.channels(), "telegram", fake)
    return fake


def _item(status="ready", source="chat", title="回覆草稿好了", seen=False, key=None):
    it = inbox.file(source=source, key=key or f"{source}:{title}:{status}", title=title)
    it.update(status=status, seen=seen)
    inbox.save(it)
    return it


class TestDigest:
    def test_unseen_work_is_summed_up_once(self, tg):
        _item(status="ready")
        _item(status="needs_approval", source="meeting", title="會議的動作")
        _item(status="fyi", title="fyi 不算")
        _item(status="ready", title="看過的不算", seen=True)
        assert asyncio.run(messengers.inbox_digest()) == 1
        [(method, kw)] = tg.sent
        assert method == "sendMessage" and kw["chat_id"] == "42"
        assert "2 件" in kw["text"] and "1 件有動作等你批准" in kw["text"] and "回覆草稿好了" in kw["text"]
        assert kw["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("d:s:")
        # the same state again says nothing; a new item speaks up
        assert asyncio.run(messengers.inbox_digest()) == 0
        _item(status="needs_input", title="新問題", key="chat:q2")
        assert asyncio.run(messengers.inbox_digest()) == 1

    def test_nothing_waiting_sends_nothing(self, tg):
        _item(status="done")
        assert asyncio.run(messengers.inbox_digest()) == 0
        assert tg.sent == []

    def test_quiet_hours_hold_it_as_a_wake(self, tg, monkeypatch):
        _item()
        monkeypatch.setattr(messengers, "quiet_until", lambda now=None: time.time() + 3600)
        assert asyncio.run(messengers.inbox_digest()) == 0
        assert tg.sent == []
        [w] = wakes.pending()
        assert w["kind"] == "quiet_push" and w["payload"]["push"] == "digest"
        # the wake fires after the hours: the digest goes out then
        monkeypatch.setattr(messengers, "quiet_until", lambda now=None: 0.0)
        def due(records):
            for r in records:
                r["due_at"] = 0.0
            return records
        wakes.wake_store().update(due)
        asyncio.run(wakes.poll())
        assert [m for m, _ in tg.sent] == ["sendMessage"]

    def test_mark_all_seen_from_the_button(self, tg):
        a, b = _item(), _item(status="needs_approval", source="meeting", title="會議的動作", key="m:1")
        assert asyncio.run(messengers.inbox_digest()) == 1
        said = asyncio.run(messengers.on_choice(tg, "42", "u", f"d:s:{int(time.time()) + 1}"))
        assert "已標為已讀（2 件）" in said
        assert inbox.get(a["id"])["seen"] and inbox.get(b["id"])["seen"]
        assert inbox.get(b["id"])["status"] == "needs_approval"   # reading is not approving

    def test_the_button_answers_only_the_personal_chat(self, tg):
        _item()
        said = asyncio.run(messengers.on_choice(tg, "999", "u", f"d:s:{int(time.time())}"))
        assert "找不到" in said


class TestStaleApprovals:
    def test_a_write_held_three_days_is_named_once_even_seen(self, tg):
        it = _item(status="needs_approval", source="meeting", title="會議的動作", seen=True)
        inbox.hold(it, "jira_add_comment", {"issue_key": "ACME-1", "body": "x"}, "meeting_tickets")
        it = inbox.get(it["id"])
        it["actions"][0]["at"] = time.time() - 4 * 86400
        inbox.save(it)
        assert asyncio.run(messengers.inbox_digest()) == 1
        text = tg.sent[0][1]["text"]
        assert "超過 3 天" in text and "會議的動作" in text and "過期" in text
        assert inbox.get(it["id"])["meta"]["stale_noted"]
        assert inbox.stale_approvals() == [] and asyncio.run(messengers.inbox_digest()) == 0
