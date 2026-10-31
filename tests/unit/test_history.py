"""Conversations from meeseek's engine reach what reads conversations back."""

import asyncio

import pytest

from meeseek import engine
from meeseek.adapters.scripted import Scripted, call, say
from meeseek.core import events as ev
from meeseek.runtime import history, orchestrator


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from meeseek.config import config

    monkeypatch.setattr(config(), "mcp_servers", [])


def _turn(monkeypatch, session, text, *steps, by="webui"):
    m = Scripted(*steps, agent="router")
    monkeypatch.setattr(engine, "build_model", lambda name, by="": m if by == orchestrator.name() else Scripted(agent=by))
    return asyncio.run(engine.turn(session, text, by=by))


def test_an_engine_turn_reads_as_adk_rows(monkeypatch):
    _turn(monkeypatch, "web-h1", "reranker 的快取命中率是多少？", call("c", "list_claude_sessions"), say("大約 62%。"))
    rows = history.rows(session="web-h1")
    assert {r.user for r in rows} == {"webui"} and len({r.turn for r in rows}) == 1
    authors = [r.event["author"] for r in rows]
    assert authors[0] == "user" and authors[-1] == orchestrator.name()
    assert history.text_of(rows[0].event) == "reranker 的快取命中率是多少？" and history.text_of(rows[-1].event) == "大約 62%。"
    assert [p["function_call"]["name"] for r in rows for p in r.event["content"]["parts"] if "function_call" in p] == \
        ["list_claude_sessions"]


def test_the_usage_page_counts_engine_model_calls(monkeypatch):
    from meeseek.runtime import usage

    store = engine.store()
    store.append([ev.UserSaid(session="web-u", turn="t", by="webui", text="hi"),
                  ev.ModelStepped(session="web-u", turn="t", by=orchestrator.name(), text="hello",
                                  usage=ev.Usage(model="gemini-2.5-flash", input_tokens=1000, output_tokens=50)),
                  ev.Replied(session="web-u", turn="t", by=orchestrator.name(), text="hello")])
    s = usage.summary(days=1)
    assert (s["totals"]["requests"], s["totals"]["prompt"], s["totals"]["output"]) == (1, 1000, 50)
    assert [r["model"] for r in s["by_model"]] == ["gemini-2.5-flash"]


def test_past_sessions_find_an_engine_conversation(monkeypatch):
    from meeseek.runtime import past_sessions

    _turn(monkeypatch, "web-h2", "幫我查 reranker 快取命中率", say("命中率 62%，比上週低。"))
    assert "web-h2" in past_sessions.search("reranker", _origin={"user": "webui"})


def test_the_inbox_finds_the_last_turn_of_an_engine_session(monkeypatch):
    from meeseek.runtime import inbox

    res = _turn(monkeypatch, "inbox-h3", "新信件", say("fyi"), by="inbox")
    assert inbox.last_invocation("inbox-h3") == res.turn
