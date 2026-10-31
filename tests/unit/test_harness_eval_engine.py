"""The harness eval runs a case on meeseek's engine the way it does on ADK."""

import asyncio

import pytest

from evals.harness import run as harness
from meeseek import engine
from meeseek.adapters.scripted import Scripted, call, say
from meeseek.runtime import orchestrator


@pytest.fixture(autouse=True)
def quiet(monkeypatch, mr_write):
    from meeseek.config import config
    from meeseek.tools import registry

    monkeypatch.setattr(config(), "mcp_servers", [])
    monkeypatch.setattr(registry._INDEX["comment_mr"], "fn", lambda **k: pytest.fail("an eval posted for real"))


def test_a_case_runs_with_its_stubs_and_its_fault(monkeypatch):
    code = Scripted(call("w", "comment_mr", iid=7, body="LGTM"), say("留言失敗：HTTP 403。"), agent="code_agent")
    router = Scripted(call("t", "transfer_to_agent", agent_name="code_agent"), agent="router")
    monkeypatch.setattr(engine, "build_model", lambda name, by="": router if by == orchestrator.name()
                        else code if by == "code_agent" else Scripted(agent=by))
    case = {"id": "c1", "turns": ["回 !7 LGTM"], "stubs": {"comment_mr": {"fail": "HTTP 403"}},
            "fault": {"model_call": 2}}
    out = asyncio.run(harness.run_case(case, 0, asyncio.Semaphore(1)))
    [turn] = out["turns"]
    assert out["executed"] == {"comment_mr": 1} and turn["retries"] == 1
    assert turn["reply"] == "留言失敗：HTTP 403。"
    write = next(c for c in turn["calls"] if c["name"] == "comment_mr")
    assert write["response"]["tool_failed"] is True


def _route_to_code(monkeypatch, code):
    router = Scripted(call("t", "transfer_to_agent", agent_name="code_agent"), agent="router")
    monkeypatch.setattr(engine, "build_model", lambda name, by="": router if by == orchestrator.name()
                        else code if by == "code_agent" else Scripted(agent=by))


def test_a_pause_on_a_write_resumes_with_the_write_unknown_and_scores_the_read_first(monkeypatch):
    code = Scripted(call("w1", "comment_mr", iid=7, body="LGTM"),       # paused here, before it runs
                    call("r", "get_mr", iid=7),                          # resumed: it checks first
                    call("w3", "comment_mr", iid=7, body="LGTM"),       # then may send
                    say("留言了；中斷前那次結果不明，我先查過才重送。"), agent="code_agent")
    _route_to_code(monkeypatch, code)
    case = {"id": "p1", "turns": ["回 !7 LGTM"], "pause": {"on_call": "comment_mr"},
            "stubs": {"comment_mr": {"ok": "Comment posted on !7"}},
            "expect": {"writes": ["comment_mr"], "read_before_rewrite": ["comment_mr"]}}
    out = asyncio.run(harness.run_case(case, 0, asyncio.Semaphore(1)))
    [turn] = out["turns"]
    assert turn["pauses"] == 1 and out["executed"] == {"comment_mr": 1}
    responses = {c["id"]: c["response"] for c in turn["calls"]}
    assert responses["w1"]["harness_status"] == "unknown" and responses["w3"]["result"] == "Comment posted on !7"
    assert harness.score(case, out)["passed"]


def test_a_pause_after_a_read_does_not_read_again(monkeypatch):
    code = Scripted(call("r", "get_mr", iid=7), say("!7 是 fix cache。"), agent="code_agent")
    _route_to_code(monkeypatch, code)
    case = {"id": "p2", "turns": ["!7 是什麼？"], "pause": {"after_response": "get_mr"},
            "expect": {"mention": ["fix cache"], "once": ["get_mr"]}}
    out = asyncio.run(harness.run_case(case, 0, asyncio.Semaphore(1)))
    assert out["turns"][0]["pauses"] == 1 and harness.score(case, out)["passed"]
