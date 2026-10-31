"""meeseek's own engine: the real skills, tools and rules, on scripted models."""

import asyncio

import pytest


from meeseek import engine
from meeseek.adapters.scripted import Scripted, call, say
from meeseek.runtime import inbox, orchestrator


@pytest.fixture(autouse=True)
def no_mcp_processes(monkeypatch):
    """The example config names real MCP servers (uvx …); these turns start none.
    tests/unit/test_mcp_adapter.py starts a real one of its own."""
    from meeseek.config import config

    monkeypatch.setattr(config(), "mcp_servers", [])


def test_every_agent_and_tool_builds():
    agents = engine.agents()
    names = [a.name for a in agents]
    assert names[0] == orchestrator.name() and len(names) == len(set(names)) > 1
    for a in agents:
        for t in a.tools:
            assert t.parameters["type"] == "object", (a.name, t.name)
            assert "tool_context" not in t.parameters["properties"], (a.name, t.name)
    research = next(a for a in agents if a.name == "research_agent")
    assert {"kb_grep", "ask_user", "propose_sop_rule", "remember_fact"} <= {t.name for t in research.tools}


def _scripted(monkeypatch, **scripts):
    """Each agent's model is the script named for it (router = the orchestrator)."""
    models = {k: Scripted(*v, agent=k) for k, v in scripts.items()}
    by_agent = {orchestrator.name() if k == "router" else k: m for k, m in models.items()}
    monkeypatch.setattr(engine, "build_model", lambda name, by="": by_agent.get(by) or Scripted(agent=by))
    return models


def test_a_turn_routes_to_a_skill_that_reads_the_knowledge_base(monkeypatch, tmp_kb):
    m = _scripted(monkeypatch,
                  router=[call("t", "transfer_to_agent", agent_name="research_agent")],
                  research_agent=[call("g", "kb_grep", pattern="Redis"), say("決議用 Redis 做 session 管理。")])
    res = asyncio.run(engine.turn("web-1", "session 管理用什麼？"))
    assert res.reply == "決議用 Redis 做 session 管理。"
    told = m["research_agent"].seen[1].messages[-1]
    assert told.role == "tool" and "doc2.md" in told.text
    assert "research" in m["research_agent"].seen[0].system.lower()
    kinds = [e.kind for e in engine.store().read("web-1")]
    assert kinds[0] == "user_said" and kinds[-1] == "replied"


def test_an_inbox_turn_holds_a_write_as_a_pending_action(monkeypatch, mr_write):
    item = inbox.file(source="chat", key="k1", title="請開單", meta={})
    item["session_id"] = "inbox-1"
    inbox.save(item)
    _scripted(monkeypatch,
              router=[call("t", "transfer_to_agent", agent_name="code_agent")],
              code_agent=[call("w", "comment_mr", iid=7, body="LGTM"), say("已列為待同意：在 !7 留言。")])
    res = asyncio.run(engine.turn("inbox-1", "請幫我回 !7", by=inbox.INBOX_USER, watched=False))
    assert [c.name for c in res.held] == ["comment_mr"]
    [action] = inbox.get(item["id"])["actions"]
    assert (action["tool"], action["status"], action["args"]) == ("comment_mr", "pending", {"iid": 7, "body": "LGTM"})


def test_a_registry_tool_gets_the_turn_it_runs_in(monkeypatch):
    from meeseek.runtime import tasks

    _scripted(monkeypatch, router=[call("c", "task_create", goal="追 CI", success="CI 綠"), say("開好了。")])
    asyncio.run(engine.turn("web-2", "開個任務追 CI"))
    [task] = tasks.open_tasks(session_id="web-2")
    assert task["session_id"] == "web-2" and task["user_id"] == "webui"


def test_the_project_wording_reaches_the_model(monkeypatch):
    m = _scripted(monkeypatch, router=[say("我晚點再回報"), say("我晚點再回報")])
    res = asyncio.run(engine.turn("web-3", "看一下"))
    assert "對帳結果" in m["router"].seen[1].messages[-1].text        # prompts/runtime/harness.md#promise
    assert res.reply.startswith("我晚點再回報")


def test_the_terminal_runs_a_turn_and_reads_it_back(monkeypatch, capsys):
    from meeseek.__main__ import main

    _scripted(monkeypatch, router=[call("g", "list_claude_sessions"), say("沒有進行中的 Claude 工作。")])
    monkeypatch.setattr("meeseek.app.build_app", lambda *a, **k: __import__("meeseek.app", fromlist=["app"]).app())
    assert main(["ask", "-s", "cli-x", "Claude 在忙什麼？"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "沒有進行中的 Claude 工作。" and "list_claude_sessions" in out.err
    assert main(["history", "cli-x"]) == 0
    assert "[you] Claude 在忙什麼？" in capsys.readouterr().out


def test_a_slash_command_that_runs_here_answers_without_a_model(monkeypatch):
    from meeseek.runtime import slash_commands

    cmd = slash_commands._BY_NAME["sync"]
    monkeypatch.setitem(slash_commands._BY_NAME, "sync",
                        type(cmd)(name="sync", args="", description="d", run=lambda: "Synced 3 docs."))
    m = _scripted(monkeypatch, router=[])
    res = asyncio.run(engine.turn("web-s", "/sync"))
    assert res.reply == "Synced 3 docs." and m["router"].seen == []
    assert [e.kind for e in engine.store().read("web-s")] == ["user_said", "replied"]


def test_a_slash_command_for_a_skill_skips_the_router(monkeypatch):
    m = _scripted(monkeypatch, router=[], sprint_agent=[say("Sprint: 12 open, 2 blocked.")])
    res = asyncio.run(engine.turn("web-h", "/healthcheck"))
    assert res.reply == "Sprint: 12 open, 2 blocked." and m["router"].seen == []


def test_the_next_message_answers_an_open_question(monkeypatch):
    from meeseek.surfaces.console import console_event

    _scripted(monkeypatch, router=[call("q", "ask_user", question="要記住 prod 叫 acme-prod 嗎？"), say("等你回答。"),
                                   say("好，不記。")])
    asyncio.run(engine.turn("web-q", "prod 叫 acme-prod"))
    asyncio.run(engine.turn("web-q", "不要"))
    said = [e for e in engine.store().read("web-q") if e.kind == "user_said"]
    assert said[0].answers == "" and said[1].answers == "q"
    shown = console_event(said[1])
    assert (shown["kind"], shown["text"], shown["answers"]) == ("said", "不要", "q")


def test_a_long_conversation_is_shown_from_a_summary(monkeypatch):
    from meeseek.config import config
    from meeseek.core.events import Summarized

    monkeypatch.setattr(config().models, "compaction_chars", 200)
    monkeypatch.setattr(config().models, "compaction_keep", 2)
    summaries = []

    async def fake_summarize(transcript, previous):
        summaries.append((transcript, previous))
        return f"S{len(summaries)}"

    monkeypatch.setattr(engine, "summarize", fake_summarize)
    m = _scripted(monkeypatch, router=[say("x" * 150) for _ in range(4)])
    for i in range(4):
        asyncio.run(engine.turn("web-long", f"question {i}"))
    stored = [e for e in engine.store().read("web-long") if isinstance(e, Summarized)]
    assert stored and all(e.upto > 0 for e in stored)
    last_seen = m["router"].seen[-1].messages
    assert last_seen[0].role == "user" and stored[-1].summary.strip() in last_seen[0].text
    assert last_seen[-1].text == "question 3"                       # the latest stays word for word
    if len(summaries) > 1:
        assert "S" + str(len(summaries) - 1) in summaries[-1][1]   # the earlier summary is folded in
    assert "question 0" in summaries[0][0]
    assert len(engine.store().read("web-long")) > len(last_seen)    # nothing is dropped from the record


def test_the_morning_plan_runs_headless_on_the_engine(monkeypatch):
    from meeseek.runtime import daily_plan
    async def collect(packs):
        return "## 信件\n(none)"

    monkeypatch.setattr(daily_plan, "_collect", collect)
    plan = Scripted(call("g", "kb_grep", pattern="sprint"), say("1. 先修 reranker"), agent="plan_my_day")
    monkeypatch.setattr(engine, "build_model", lambda name, by="": plan)
    out = daily_plan.plan_my_day()
    assert out.startswith("1. 先修 reranker") and "daily_plan" in out
    assert "generate_image" not in plan.seen[0].tools and "kb_grep" in plan.seen[0].tools
    [sid] = [s for s in engine.store().sessions() if s.startswith("workflow.")]
    assert engine.store().read(sid)[0].by == "workflow"


def test_a_turn_is_recorded_for_the_learning_loop_and_a_correction_is_noticed(monkeypatch):
    from meeseek.runtime import feedback, trajectory

    _scripted(monkeypatch,
              router=[call("t", "transfer_to_agent", agent_name="sprint_agent")],
              sprint_agent=[call("w", "jira_get_issue", issue_key="ACME-1"), say("ACME-1 在 Review。"),
                            say("抱歉，ACME-1 其實已經 Done。")])
    first = asyncio.run(engine.turn("web-l", "ACME-1 狀態？"))
    [rec] = trajectory.turns_store().read()
    assert (rec["inv"], rec["skill"], rec["user"], rec["request"]) == (first.turn, "sprint", "webui", "ACME-1 狀態？")
    assert rec["reply_head"] == "ACME-1 在 Review。" and rec["policy"] and rec["flags"] == {}
    asyncio.run(engine.turn("web-l", "不對，你再查一次，它應該已經關了"))
    assert any(o["inv"] == first.turn and o["kind"] in ("correction", "reask") for o in feedback.read())


def test_a_rule_written_in_a_turn_keeps_the_conversation_that_led_to_it(monkeypatch):
    from meeseek.runtime import cases, learned_rules

    _scripted(monkeypatch,
              router=[call("t", "transfer_to_agent", agent_name="sprint_agent")],
              sprint_agent=[say("開好了：[ACME] Fix reranker"),
                            call("p", "propose_sop_rule", rule="開 Jira 單時標題一律用中文摘要，不要加英文前綴",
                                 reason="使用者糾正標題格式"), say("記下了。")])
    asyncio.run(engine.turn("web-r", "幫我開一張修 reranker 的單"))
    asyncio.run(engine.turn("web-r", "以後標題都用中文，幫我寫成規則"))
    [rule] = learned_rules.parse_learned("sprint")
    assert "中文摘要" in rule.text
    case = learned_rules.cases_dir() / "sprint" / f"{learned_rules.rule_id(rule.text)}.json"
    import json

    kept = json.loads(case.read_text())
    assert kept["turns"][0]["user_content"]["parts"][0]["text"] == "幫我開一張修 reranker 的單"
    assert any(c["origin"].startswith("rule:") for c in cases.load("sprint"))
