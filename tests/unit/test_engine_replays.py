"""Replays on meeseek's engine: the real agents, nothing with an effect run for real."""

import asyncio

import pytest

from meeseek import engine
from meeseek.adapters.scripted import Scripted, call, say
from meeseek.runtime import orchestrator, replays


@pytest.fixture(autouse=True)
def on_the_engine(monkeypatch, mr_write):
    from meeseek.config import config
    from meeseek.tools import registry
    monkeypatch.setattr(config(), "mcp_servers", [])
    monkeypatch.setattr(registry._INDEX["comment_mr"], "fn", lambda **k: pytest.fail("a replay posted for real"))


def _scripted(monkeypatch, **scripts):
    models = {orchestrator.name() if k == "router" else k: Scripted(*v, agent=k) for k, v in scripts.items()}
    monkeypatch.setattr(engine, "build_model", lambda name, by="": models.get(by) or Scripted(agent=by))
    return models


PREFIX = [  # a case's earlier events, in the shape ADK stores them
    {"author": "user", "invocation_id": "i0", "timestamp": 1.0, "content": {"parts": [{"text": "幫我看 !7"}]}},
    {"author": "code_agent", "invocation_id": "i0", "timestamp": 2.0,
     "content": {"parts": [{"function_call": {"id": "f1", "name": "get_mr", "args": {"iid": 7}}}]}},
    {"author": "code_agent", "invocation_id": "i0", "timestamp": 3.0,
     "content": {"parts": [{"function_response": {"id": "f1", "name": "get_mr", "response": {"result": "!7: fix cache"}}}]}},
    {"author": "code_agent", "invocation_id": "i0", "timestamp": 4.0, "content": {"parts": [{"text": "!7 修快取。"}]}},
]


def test_a_conversation_is_replayed_on_its_context_with_writes_stubbed(monkeypatch):
    m = _scripted(monkeypatch, code_agent=[call("w", "comment_mr", iid=7, body="LGTM"), say("留言失敗：HTTP 403。")])
    run = asyncio.run(replays.run_session(["那就回 LGTM"], prefix_events=PREFIX,
                                          stubs={"comment_mr": {"fail": "HTTP 403"}}))
    assert run.error == "" and run.executed == {"comment_mr": 1}
    [turn] = run.turns
    assert turn.reply == "留言失敗：HTTP 403。" and turn.calls == [{"name": "comment_mr", "args": {"iid": 7, "body": "LGTM"},
                                                               "failed": True}]
    seen = [(x.role, x.text) for x in m["code_agent"].seen[0].messages]
    assert seen[0] == ("user", "幫我看 !7") and ("assistant", "!7 修快取。") in seen   # the case's context, as it was


def test_under_a_bundle_the_model_reads_its_descriptions(monkeypatch):
    from meeseek.runtime import policy

    m = _scripted(monkeypatch, router=[say("ok")])
    bundle = policy.overlay(tool_descriptions={"list_claude_sessions": "CANDIDATE wording"})
    asyncio.run(replays.run_session(["hi"], bundle=bundle))
    assert m[orchestrator.name()].seen[0].descriptions["list_claude_sessions"] == "CANDIDATE wording"


def test_a_replay_leaves_no_turn_on_the_record(monkeypatch):
    from meeseek.runtime import trajectory

    _scripted(monkeypatch, router=[say("ok")])
    out = asyncio.run(replays.replay("hi"))
    assert out.reply == "ok" and out.error == "" and trajectory.turns_store().read() == []
    assert engine.store().sessions() == []          # in memory: the real store never sees it
