"""The policy as a version: hashed for provenance, overridden for a replay, reloaded without a restart."""

import asyncio

import pytest

from meeseek import engine
from meeseek.core.turn import TurnState
from meeseek.runtime import orchestrator, policy

_AGENTS = {}


def _agent(name):
    if not _AGENTS:
        _AGENTS.update({a.name: a for a in engine.agents()})
    return _AGENTS[orchestrator.name() if name == "router" else name]


def _instruction(agent) -> str:
    return agent.system(TurnState(session="s", turn="t", watched=True, started=0.0, history=[], agent=agent.name))


def test_a_bundle_changes_a_skill_only_inside_its_replay():
    sprint = _agent("sprint_agent")
    before = _instruction(sprint)
    with policy.using(policy.overlay(skills={"sprint": "你是測試用的 sprint SOP。"})):
        assert _instruction(sprint).startswith("你是測試用的 sprint SOP。")
        assert "測試用的" not in _instruction(_agent("code_agent"))
    assert _instruction(sprint) == before                     # a person's turn: the policy on disk


def test_a_bundle_can_override_the_router_prompt_and_a_skills_rules():
    from meeseek.runtime.learned_rules import load_learned_rules

    with policy.using(policy.overlay(prompts={"agents/router": "ROUTER-V2 {{slash_tool_commands}}"},
                                     rules={"sprint": ["- 開單前先查重複的票 _（2026-10-21，console，依據：x）_"]})):
        assert _instruction(_agent("router")).startswith("ROUTER-V2")
        assert "先查重複的票" in load_learned_rules("sprint")


def test_concurrent_replays_each_see_their_own_bundle():
    sprint = _agent("sprint_agent")

    async def run(tag):
        with policy.using(policy.overlay(skills={"sprint": f"SOP-{tag}"})):
            await asyncio.sleep(0)
            return _instruction(sprint).split("\n", 1)[0]

    async def both():
        return await asyncio.gather(run("A"), run("B"))

    assert asyncio.run(both()) == ["SOP-A", "SOP-B"]


def test_an_edited_skill_is_live_on_the_next_turn_without_a_rebuild(monkeypatch):
    """The engine reads each agent's instruction when it answers, so an edit
    to SKILL.md (a graduated rule, an approved proposal) needs no restart."""
    sprint = _agent("sprint_agent")
    monkeypatch.setattr(policy, "skill_body", lambda name, path, fallback: "EDITED" if name == "sprint" else fallback)
    assert _instruction(sprint).startswith("EDITED")


def test_a_bundle_rewrites_a_read_tools_description_never_a_write_tools(mr_write):
    from meeseek.adapters.scripted import Scripted, say

    inner = Scripted(say("ok"))
    tools = [engine.registry_spec("kb_grep"), engine.registry_spec("comment_mr")]
    with policy.using(policy.overlay(tool_descriptions={"kb_grep": "new grep", "comment_mr": "new comment"})):
        asyncio.run(engine._Bundled(inner).step(system="", history=[], tools=tools, session="s", turn="t"))
    seen = inner.seen[0].descriptions
    assert seen["kb_grep"] == "new grep" and seen["comment_mr"] == engine.registry_spec("comment_mr").description


@pytest.mark.parametrize("target,ok", [
    ("skill:sprint", True), ("desc:code", True), ("rules:research", True),
    ("prompt:agents/router", True), ("prompt:blocks/common", True), ("tool:kb_grep", True),
    ("prompt:internal/turn_judge", False), ("prompt:runtime/consent#ask_first", False),
    ("prompt:blocks/memory", False), ("tool:comment_mr", False), ("tool:jira_create_issue", False),
    ("skill:nope", False), ("frontmatter:sprint", False), ("rubric:sprint", False),
])
def test_what_a_proposal_may_change(target, ok):
    assert policy.editable(target) is ok


def test_the_manifest_names_each_component_and_the_bundle_follows_it():
    m = policy.manifest()
    assert {"skill:sprint", "desc:sprint", "prompt:agents/router", "prompt:blocks/common"} <= set(m)
    assert len(policy.bundle_id()) == 8
    assert set(policy.components_of(["sprint_agent"])) == {"skill:sprint", "desc:sprint", "prompt:agents/router",
                                                            "prompt:blocks/common"}
