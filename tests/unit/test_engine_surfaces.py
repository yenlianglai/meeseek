"""The Inbox and the MCP surface on meeseek's engine."""

import asyncio

import pytest

from meeseek import engine
from meeseek.adapters.scripted import Scripted, call, say
from meeseek.runtime import inbox, mcp_server, orchestrator


@pytest.fixture(autouse=True)
def on_the_engine(monkeypatch, mr_write):
    from meeseek.config import config
    monkeypatch.setattr(config(), "mcp_servers", [])


def _scripted(monkeypatch, **scripts):
    models = {orchestrator.name() if k == "router" else k: Scripted(*v, agent=k) for k, v in scripts.items()}
    monkeypatch.setattr(engine, "build_model", lambda name, by="": models.get(by) or Scripted(agent=by))
    return models


def test_an_inbox_write_waits_runs_once_approved_and_is_settled(monkeypatch):
    from meeseek.tools import registry

    posted = []
    monkeypatch.setattr(registry._INDEX["comment_mr"], "fn",
                        lambda iid, body, project="", **k: posted.append((iid, body)) or f"Comment posted on !{iid}")
    item = inbox.file(source="chat", key="k-mr", title="幫我回 !7", meta={})
    _scripted(monkeypatch,
              router=[call("t", "transfer_to_agent", agent_name="code_agent")],
              code_agent=[call("w", "comment_mr", iid=7, body="LGTM"), say("已列為待同意：在 !7 留言 LGTM。"),
                          call("w2", "comment_mr", iid=7, body="LGTM"), say("已在 !7 留言。")])
    asyncio.run(inbox.work(item["id"], "幫我回 !7"))
    item = inbox.get(item["id"])
    assert item["status"] == "needs_approval" and posted == []
    [action] = item["actions"]

    message = inbox.approve(item["id"], action["id"])
    asyncio.run(inbox.work(item["id"], message))
    item = inbox.get(item["id"])
    assert posted == [(7, "LGTM")]
    assert item["actions"][0]["status"] == "done" and "Comment posted on !7" in item["actions"][0]["result"]
    assert item["status"] == "ready" and item["reply"] == "已在 !7 留言。"


def test_an_mcp_turn_reports_the_question_it_left_open(monkeypatch):
    _scripted(monkeypatch, router=[call("q", "ask_user", question="哪個環境？", options=["uat", "prod"]),
                                   say("等你回答。")])
    reply, by, needs = asyncio.run(mcp_server.run_orchestrator_turn("看一下 log", "mcp-1", user_id="mcp"))
    assert (reply, by) == ("等你回答。", orchestrator.name())
    assert needs == ["哪個環境？\n選項：uat / prod"]
