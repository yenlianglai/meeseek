"""Triggers: what a source files, what needs no model to settle, and what
starts an agent turn."""

import asyncio
import time

import pytest

from meeseek.runtime import inbox, triggers, turn_budget




def _mail(**meta):
    return {"source": "gmail", "key": "gmail:m1", "title": "請幫忙看 reranker", "sender": "a@x.com",
            "body": "排序怪怪的", "link": "", "received_at": time.time(),
            "meta": {"thread_id": "t1", "bulk": False, "invite": False, **meta}}


def _source(monkeypatch, name, poll):
    """The one source, named `name`, answering `poll(state, now)` (as a pack's would)."""
    from meeseek.config import config

    monkeypatch.setattr(config().triggers, "sources", {name: {"tool": f"{name}_poll"}})
    monkeypatch.setattr(triggers, "_poller", lambda n: poll if n == name else pytest.fail(f"polled {n}"))


class TestSettledWithoutAModel:
    def test_decisions(self, monkeypatch):
        monkeypatch.setattr(triggers, "triage", lambda *a: pytest.fail("no model for these"))
        assert triggers.decide(_mail(bulk=True))["action"] == "ignore"
        assert triggers.decide(_mail(invite=True))["action"] == "fyi"
        assert triggers.decide({"source": "alert", "title": "x", "meta": {}})["action"] == "investigate"
        assert triggers.decide({"source": "calendar", "title": "x", "meta": {}})["action"] == "brief"


class TestPoll:
    def test_work_starts_only_for_what_needs_it(self, monkeypatch):
        started = []
        _source(monkeypatch, "gmail", lambda state, now: [
            _mail(), {**_mail(bulk=True, thread_id="t2"), "key": "gmail:m2"}])
        monkeypatch.setattr(triggers, "triage", lambda *a: {"action": "reply", "summary": "s", "why": "對方在等回覆"})
        monkeypatch.setattr(inbox, "start", lambda item_id, message: started.append((item_id, message)))

        out = asyncio.run(triggers.poll())
        assert len(out["filed"]) == 2 and out["status"]["gmail"]["ok"]
        [(item_id, message)] = started
        assert "請幫忙看 reranker" in message and "```reply" in message
        statuses = sorted(i["status"] for i in inbox.items())
        assert statuses == ["ignore", "new"]   # the turn is started, not yet run

        assert asyncio.run(triggers.poll())["filed"] == []   # filed once

    def test_past_the_daily_cap_items_file_as_fyi(self, monkeypatch):
        from meeseek.config import cfg

        monkeypatch.setattr(cfg.triggers, "daily_turns", 1)
        _source(monkeypatch, "gmail", lambda state, now: [
            _mail(), {**_mail(thread_id="t2"), "key": "gmail:m2"}])
        monkeypatch.setattr(triggers, "triage", lambda *a: {"action": "reply", "summary": "s", "why": "等回覆"})
        started = []
        monkeypatch.setattr(inbox, "start", lambda item_id, message: started.append(item_id))

        out = asyncio.run(triggers.poll())
        assert len(out["filed"]) == 2 and len(started) == 1   # the second is past the cap
        assert sorted(i["status"] for i in inbox.items()) == ["fyi", "new"]
        fyi = next(i for i in inbox.items() if i["status"] == "fyi")
        assert "上限" in fyi["triage"]["why"]
        assert turn_budget.turns_today() == 1

    def test_no_cap_when_daily_turns_is_zero(self, monkeypatch):
        from meeseek.config import cfg

        monkeypatch.setattr(cfg.triggers, "daily_turns", 0)
        _source(monkeypatch, "gmail", lambda state, now: [
            _mail(), {**_mail(thread_id="t2"), "key": "gmail:m2"}])
        monkeypatch.setattr(triggers, "triage", lambda *a: {"action": "reply", "summary": "s", "why": "等回覆"})
        started = []
        monkeypatch.setattr(inbox, "start", lambda item_id, message: started.append(item_id))

        asyncio.run(triggers.poll())
        assert len(started) == 2

    def test_a_conversation_is_one_item_your_reply_settles(self, monkeypatch):
        """A Chat space is one item: the next message updates it, and once you
        wrote there it is done."""
        def chat(key, text, at, answered=()):
            return {"items": [{"kind": "chat", "key": key, "group": "spaces/A", "title": f"DM: {text}",
                               "sender": "Heidi", "body": text, "received_at": at,
                               "meta": {"space": "spaces/A", "dm": True}}],
                    "answered": list(answered)}

        replies = iter([chat("chat:1", "在嗎", 100.0), chat("chat:2", "幫我看一下 log", 200.0),
                        {"items": [], "answered": [{"group": "spaces/A", "at": 300.0}]}])

        def poll(state, now):
            data = next(replies)
            for a in data["answered"]:
                inbox.answered(a["group"], a["at"])
            return [{"source": "chat", **i} for i in data["items"]]

        _source(monkeypatch, "chat", poll)
        monkeypatch.setattr(triggers, "triage", lambda *a: {"action": "reply", "summary": "s", "why": "等回覆"})
        started = []
        monkeypatch.setattr(inbox, "start", lambda item_id, message: started.append(message))

        asyncio.run(triggers.poll())
        [item] = inbox.items()
        item["status"] = "ready"            # its turn ran
        inbox.save(item)
        asyncio.run(triggers.poll())
        [item] = inbox.items()              # still one
        assert "在嗎" in item["body"] and "幫我看一下 log" in item["body"] and item["title"] == "DM: 幫我看一下 log"
        assert inbox.by_key("chat:2")["id"] == item["id"] and not item["seen"]
        assert len(started) == 2 and "幫我看一下 log" in started[1] and "在嗎" not in started[1]   # the turn is on the news
        item["status"] = "ready"
        inbox.save(item)
        asyncio.run(triggers.poll())
        assert inbox.get(item["id"])["status"] == "done"

    def test_news_asking_nothing_keeps_a_waiting_item_waiting(self, monkeypatch):
        verdicts = iter([{"action": "reply", "summary": "s", "why": "w"}, {"action": "fyi", "summary": "s", "why": "w"}])
        monkeypatch.setattr(triggers, "triage", lambda *a: next(verdicts))
        monkeypatch.setattr(inbox, "start", lambda item_id, message: None)
        _source(monkeypatch, "gmail", lambda state, now: [_mail()])
        asyncio.run(triggers.poll())
        [item] = inbox.items()
        item["status"] = "ready"
        inbox.save(item)
        _source(monkeypatch, "gmail", lambda state, now: [{**_mail(), "key": "gmail:m2", "body": "謝謝"}])
        asyncio.run(triggers.poll())
        [item] = inbox.items()
        assert item["status"] == "ready" and "謝謝" in item["body"]

    def test_a_broken_source_is_reported_not_raised(self, monkeypatch):

        def broken(state, now):
            raise RuntimeError("Request had insufficient authentication scopes.")

        _source(monkeypatch, "chat", broken)
        out = asyncio.run(triggers.poll())
        assert not out["status"]["chat"]["ok"] and "setup.sh" in out["status"]["chat"]["error"]

    def test_every_source_has_a_message(self):
        common = {"triage": {"why": "w"}, "title": "t", "sender": "s", "body": "b"}
        for item in [
            {**common, "source": "gmail", "meta": {"thread_id": "t1"}},
            {**common, "source": "chat", "meta": {"space": "spaces/A", "dm": True}},
            {**common, "source": "gitlab", "kind": "mr", "link": "https://g/x/-/merge_requests/1",
             "meta": {"project": "x", "iid": 1, "role": "reviewer"}},
            {**common, "source": "jira", "kind": "jira", "link": "https://acme.atlassian.net/browse/ACME-1",
             "meta": {"issue_key": "ACME-1", "role": "指派給你"}},
            {**common, "source": "alert", "meta": {"project": "p", "opened": "2026-10-10T01:00:00Z",
                                                   "subject": "k8s_container", "policy": "x"}},
            {**common, "source": "calendar", "meta": {"start": "2026-10-10T10:00:00+08:00",
                                                      "end": "2026-10-10T11:00:00+08:00",
                                                      "attendees": ["Heidi"]}},
        ]:
            assert "{{" not in triggers.message_for(item)


def _notice(key, sender, title, body=""):
    return {"source": "gmail", "kind": "mail", "key": f"gmail:{key}", "title": title, "sender": sender, "body": body,
            "link": "", "received_at": time.time(), "group": f"thread:{key}",
            "meta": {"thread_id": key, "bulk": True, "invite": False}}


_JIRA_MAIL = '"Heidi (Jira)" <jira@acme.atlassian.net>'
_GITLAB_MAIL = '"Heidi (@heidi)" <gitlab@gitlab.acme.test>'


class TestOtherConversations:
    def test_which_ticket_or_mr_a_notification_is_about(self):
        assert triggers.notice_group(_notice("g1", _JIRA_MAIL, "[JIRA] (ACME-17) 修登入")) == "jira:ACME-17"
        assert triggers.notice_group(_notice("g2", _JIRA_MAIL, "[JIRA] Heidi 已將 ACME-18 指派給您")) == "jira:ACME-18"
        assert triggers.notice_group(_notice("g3", "Jira <jira@acme.atlassian.net>", "這是您的每週更新")) == ""
        mr = _notice("g4", _GITLAB_MAIL, "Re: App | 修登入 (!7)",
                     "Heidi commented: https://gitlab.acme.test/grp/sub/app/-/merge_requests/7#note_1")
        assert triggers.notice_group(mr) == "mr:grp/sub/app!7"
        pipeline = _notice("g5", "Acme-Gitlab <gitlab@gitlab.acme.test>", "App | Failed pipeline for main",
                           "Pipeline #9 has failed! https://gitlab.acme.test/grp/app/-/pipelines/9")
        assert triggers.notice_group(pipeline) == ""
        assert triggers.notice_group(_notice("g6", "a@acme.test", "(ACME-1) 問一下")) == ""   # a person's mail

    def test_a_notification_waits_with_its_ticket_and_the_tickets_source_takes_over(self, monkeypatch):
        monkeypatch.setattr(triggers, "triage", lambda *a: {"action": "reply", "summary": "s", "why": "問你"})
        started = []
        monkeypatch.setattr(inbox, "start", lambda item_id, message: started.append(message))
        jira = {"source": "jira", "kind": "jira", "key": "jira:ACME-17:11", "group": "jira:ACME-17",
                "title": "ACME-17 修登入", "sender": "Heidi", "body": "Heidi: 這張還要做嗎？", "received_at": time.time(),
                "link": "https://acme.atlassian.net/browse/ACME-17", "meta": {"issue_key": "ACME-17", "role": "指派給你"}}
        _source(monkeypatch, "mix", lambda state, now: [_notice("g1", _JIRA_MAIL, "[JIRA] (ACME-17) 修登入")])
        asyncio.run(triggers.poll())
        [item] = inbox.items()
        assert item["group"] == "jira:ACME-17" and item["status"] == "fyi" and not started   # not ignored, not a turn
        _source(monkeypatch, "mix", lambda state, now: [jira, _notice("g2", _JIRA_MAIL, "[JIRA] (ACME-17) 修登入")])
        asyncio.run(triggers.poll())
        [item] = inbox.items()
        assert item["kind"] == "jira" and item["source"] == "jira" and "notice" not in item["meta"]
        assert set(item["keys"]) == {"jira:ACME-17:11", "gmail:g2"} and item.get("notices") == 1
        [message] = started
        assert "jira_get_issue" in message and "這張還要做嗎" in message

    def test_a_notification_leaves_a_waiting_mr_item_as_it_was(self, monkeypatch):
        it = inbox.file(source="gitlab", kind="mr", key="mr:grp/app!7:opened", group="mr:grp/app!7",
                        title="grp/app!7 修登入", body="Heidi 開的 MR")
        it.update(status="ready", seen=True)
        inbox.save(it)
        _source(monkeypatch, "gmail", lambda state, now: [_notice(
            "g1", _GITLAB_MAIL, "Re: App | 修登入 (!7)", "Heidi pushed new commits https://gitlab.acme.test/grp/app/-/merge_requests/7")])
        monkeypatch.setattr(triggers, "triage", lambda *a: pytest.fail("a notification asks no model"))
        assert asyncio.run(triggers.poll())["filed"] == []
        after = inbox.get(it["id"])
        assert after["status"] == "ready" and after["seen"] and after["title"] == "grp/app!7 修登入"
        assert "通知信" in after["body"] and inbox.by_key("gmail:g1")["id"] == it["id"]

    def test_a_chat_message_linking_an_mr_files_into_the_mr_and_drafts_a_review(self, monkeypatch):
        started = []
        monkeypatch.setattr(inbox, "start", lambda item_id, message: started.append(message))
        monkeypatch.setattr(triggers, "triage", lambda *a: pytest.fail("a review asked needs no model"))
        chat = {"source": "chat", "kind": "chat", "key": "chat:m1", "group": "spaces/DM1", "title": "DM: 幫看",
                "sender": "Heidi", "received_at": time.time(), "link": "",
                "body": "Heidi: 幫我看一下 https://gitlab.acme.test/grp/app/-/merge_requests/7 謝謝",
                "meta": {"space": "spaces/DM1", "dm": True}}
        _source(monkeypatch, "chat", lambda state, now: [chat])
        asyncio.run(triggers.poll())
        [item] = inbox.items()
        assert item["group"] == "mr:grp/app!7" and item["kind"] == "mr"
        assert item["link"] == "https://gitlab.acme.test/grp/app/-/merge_requests/7"
        assert item["triage"]["why"] == "Review asked in Chat"
        [message] = started
        assert "review_context" in message and "第一輪 review" in message

    def test_a_chat_message_with_two_mrs_stays_a_chat(self):
        body = "https://g.test/a/b/-/merge_requests/1 和 https://g.test/a/b/-/merge_requests/2"
        assert triggers.chat_mr({"source": "chat", "kind": "chat", "body": body, "meta": {}}) is None

    def test_each_poll_lets_stale_items_go(self, monkeypatch):
        seen = []
        monkeypatch.setattr(inbox, "expire", lambda now, digest: seen.append(digest) or 0)
        _source(monkeypatch, "gmail", lambda state, now: [])
        asyncio.run(triggers.poll())
        assert seen == [False]   # the example config sends no digest: a stale write expires unnamed


def test_a_disabled_api_says_where_to_enable_it():
    e = RuntimeError('returned "Google Calendar API has not been used in project acme-sandbox before or it '
                     'is disabled. Enable it by visiting ..."')
    msg = triggers._explain(e)
    assert "acme-sandbox" in msg and "Calendar" in msg and "enable" in msg
