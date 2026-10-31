"""The harness's classification of tools, and the failures it reads in their results.

Its rules about replies — a failed write reported, a promise kept — are the
core's policies now (tests/unit/test_turn_runner.py).
"""

import asyncio

from meeseek.core import events as ev
from meeseek.core.ledger import failure_of
from meeseek.core.policies import FailedWriteCheck
from meeseek.core.turn import TurnState
from meeseek.runtime import harness


class _Packs:
    """What the running packs declared (adapters/mcp.McpTools.effects)."""

    def effects(self):
        return {"comment_mr_lines": "write", "get_mr": "read", "sync_repo": "side"}


def _with_packs():
    from meeseek.app import build_app

    a = build_app()
    a.mcp_tools = _Packs()
    return a.active()


def test_classification():
    assert harness.is_write("jira_link_to_epic"), "an MCP server's filtered tool that does not read"
    assert harness.is_write("propose_sop_rule")
    assert not harness.is_write("jira_get_issue") and not harness.is_write("kb_grep")
    with _with_packs():
        assert harness.is_write("comment_mr_lines") and not harness.is_write("get_mr")
    assert not harness.is_write("comment_mr_lines"), "no pack running: nothing declared it"


def test_has_effect_covers_writes_and_side_effects():
    for name in ("jira_create_issue", "propose_sop_rule", "sync", "sync_drive", "process_meetings",
                 "generate_image", "ship_output", "update_kb_file"):
        assert harness.has_effect(name), name
    assert not harness.has_effect("kb_grep") and not harness.has_effect("jira_get_issue")
    with _with_packs():
        assert harness.has_effect("comment_mr_lines") and harness.has_effect("sync_repo")
        assert not harness.has_effect("get_mr")


# Tools that are neither reads nor effects: the question to the person, the
# hand-off, web search, and orchestration that only calls reads.
_HARMLESS = {"ask_user", "transfer_to_agent", "web_search", "plan_my_day"}


def test_every_tool_with_an_effect_an_agent_holds_is_stubbed_in_a_replay(mr_write):
    """A replay stubs exactly the tools `has_effect` names, so a tool that
    changes something and is missing there would run for real in a replay."""
    from meeseek import engine
    from meeseek.config import cfg
    from meeseek.tools.registry import GENERAL_TOOLS, READ_PREFIXES

    names = {t.name for a in engine.agents() for t in a.tools}
    names |= {n for server in cfg.mcp_servers for n in server.tool_filter}     # what MCP packs add
    assert {"ship_output", "sync", "sync_drive", "comment_mr"} <= names, "found too little"
    unstubbed = sorted(n for n in names if n not in GENERAL_TOOLS and not n.startswith(READ_PREFIXES)
                       and n not in _HARMLESS and not harness.has_effect(n))
    assert unstubbed == [], f"tools with an effect that a replay would run for real: {unstubbed}"


def test_a_registry_write_that_reports_failure_in_text_is_a_failure():
    """GitLab and Jira writes return "Failed to …" rather than raising, and a
    batch says "K FAILED" on its first line; the ledger must see both."""
    assert failure_of({"result": "Failed to comment on !5: HTTP 403"})
    assert failure_of({"result": "1 of 2 comments posted on !5; 1 FAILED:\n- x"})
    assert failure_of({"result": "Nothing was posted:\n- comment 1: needs file_path"})
    assert not failure_of({"result": "2 of 2 comments posted on !5:\n- x"})
    assert not failure_of({"result": "Comment posted on !5 (note #1)"})


def test_a_failure_explained_in_an_ask_user_question_is_reported():
    link = ev.ToolCall(id="w", name="jira_link_to_epic", args={"issue_key": "ACME-17766"})
    q = ev.ToolCall(id="q", name="ask_user", args={"question": "掛 Epic 失敗：ACME-17766 不是 Epic，要改掛哪一張？"})
    events = [ev.ModelStepped(session="s", turn="t", calls=(link,)),
              ev.ToolRan(session="s", turn="t", call=link, result={"result": "Failed: not an epic"}),
              ev.ModelStepped(session="s", turn="t", calls=(q,)),
              ev.ToolRan(session="s", turn="t", call=q, result={"status": "waiting"})]
    t = TurnState(session="s", turn="t", watched=True, started=0.0, history=[], events=events)
    assert asyncio.run(FailedWriteCheck(harness.is_write).on_reply(t, "等你回覆。")) is None
