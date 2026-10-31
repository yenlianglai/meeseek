"""meeseek's own past conversations: found by what was said, read back as turns."""

import json
import sqlite3

import pytest

from meeseek.runtime import past_sessions
from meeseek.runtime.sessions import APP_NAME
from meeseek.config.paths import home

NOW = 1_800_000_000.0


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = home().sessions_db
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE events (id TEXT, app_name TEXT, user_id TEXT, session_id TEXT, "
                 "invocation_id TEXT, timestamp REAL, event_data TEXT)")
    n = [0]

    def add(user, session, author, text="", call="", at=0.0):
        n[0] += 1
        parts = [{"function_call": {"name": call, "args": {}}}] if call else [{"text": text}]
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                     (str(n[0]), APP_NAME, user, session, "inv", NOW - 3600 + at + n[0],
                      json.dumps({"author": author, "content": {"parts": parts}})))
        conn.commit()

    monkeypatch.setattr(past_sessions.time, "time", lambda: NOW)
    monkeypatch.setattr("meeseek.runtime.inbox.by_session", lambda sid: None)
    return add


def test_the_conversation_the_user_had_outranks_long_ones_that_name_everyone(db):
    db("webui", "review", "user", "幫我 code review ACME-17835-npc-engine-ws")
    db("webui", "review", "code_agent", "MR !170 作者 Ovien，留了 6 則 comment")
    # the morning plan's raw data: every name, every status, many times
    db("workflow", "plan", "user", "以下是今天的原始資料 " + "Ovien Code Review " * 50)
    db("workflow", "plan", "planner", "今日計畫")
    out = past_sessions.search("ovien review")
    assert out.index("`review`") < out.index("`plan`")


def test_every_word_must_be_there_and_the_asking_session_is_left_out(db):
    db("webui", "a", "user", "jev 壓測結果")
    db("webui", "b", "user", "jev 的 reranker")
    db("webui", "here", "user", "找一下之前 jev 壓測的對話")
    out = past_sessions.search("jev 壓測", _origin={"user": "webui", "session_id": "here"})
    assert "`a`" in out and "`b`" not in out and "`here`" not in out


def test_the_users_words_are_shown_not_the_wrapper_around_them(db):
    db("inbox", "phone", "user", "使用者從手機（Telegram）傳訊息給你，這是使用者本人的請求。\n"
                                "- 你的回覆會以純文字傳回手機\n\n有一個幫 ovien review 的 session")
    out = past_sessions.search("ovien")
    assert "一開始問：有一個幫 ovien review 的 session" in out and "/ui#/chat/phone?as=inbox" in out


def test_nothing_found_says_how_to_look_again(db):
    db("webui", "a", "user", "hello")
    assert "換個關鍵字" in past_sessions.search("guardrail")


def test_a_session_reads_back_as_turns_with_the_tools_it_used(db):
    db("webui", "s", "user", "ACME-17893 現在什麼狀態")
    db("webui", "s", "sprint_agent", call="jira_get_issue")
    db("webui", "s", "sprint_agent", call="jira_search")
    db("webui", "s", "sprint_agent", "在 Code Review")
    out = past_sessions.read("s")
    assert "**使用者**：ACME-17893 現在什麼狀態" in out
    assert "（查了 jira_get_issue, jira_search）" in out and "在 Code Review" in out
    assert "找不到" in past_sessions.read("nope")


def test_an_mcp_caller_sees_its_own_sessions_only(db):
    db("webui", "console", "user", "gateway 設計")
    db("mcp:other", "theirs", "user", "gateway 設計")
    db("mcp:me", "mine", "user", "gateway 設計")
    out = past_sessions.search("gateway", _origin={"user": "mcp:me", "session_id": "now"})
    assert "`mine`" in out and "`console`" not in out and "`theirs`" not in out
    assert "找不到" in past_sessions.read("console", _origin={"user": "mcp:me"})
    assert "`console`" in past_sessions.search("gateway", _origin={"user": "webui", "session_id": "now"})
