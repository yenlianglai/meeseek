"""The agents meeseek's engine builds: who answers, with what instruction and which tools."""

import pytest

from meeseek import engine
from meeseek.core.turn import TurnState
from meeseek.runtime import orchestrator
from meeseek.runtime.skills import discover_skills


@pytest.fixture(scope="module")
def built():
    return {a.name: a for a in engine.agents()}


def _state(agent="", by="webui"):
    return TurnState(session="s", turn="t", watched=True, started=0.0, history=[], agent=agent, by=by)


def _tools(agent):
    return {t.name for t in agent.tools}


def test_the_router_and_one_agent_per_skill(built):
    names = list(built)
    assert names[0] == orchestrator.name()
    assert sorted(names[1:]) == sorted(s.agent_name for s in discover_skills())
    assert all(n.isidentifier() for n in names) and all(built[n].description for n in names)


def test_slash_commands_go_to_real_agents(built):
    from meeseek.runtime.slash_commands import COMMANDS

    assert sorted({c.target for c in COMMANDS if c.skill} - set(built)) == []


def test_every_agent_hears_the_memory_each_turn(built, monkeypatch):
    from meeseek.context import memory

    monkeypatch.setattr(memory, "load_pinned_notes", lambda: "PINNED-MARKER")
    for a in built.values():
        assert "PINNED-MARKER" in a.system(_state(a.name)), a.name


def test_every_agent_gets_the_common_block_once(built):
    from meeseek.config import PROMPTS_DIR, home

    marker = "## 使用者附的圖"
    for sop in [*home().skills.glob("*/SKILL.md"), PROMPTS_DIR / "agents" / "router.md"]:
        assert marker not in sop.read_text(encoding="utf-8"), sop
    for a in built.values():
        assert a.system(_state(a.name)).count(marker) == 1, a.name


def test_tool_names_are_clean_unique_and_described(built):
    for a in built.values():
        names = [t.name for t in a.tools]
        assert len(names) == len(set(names)), a.name
        for t in a.tools:
            assert t.name.replace("_", "a").isalnum(), (a.name, t.name)
            assert t.description.strip(), (a.name, t.name)
            assert t.parameters.get("type") == "object", (a.name, t.name)


def test_people_facing_agents_ask_search_and_remember(built):
    for a in built.values():
        assert {"ask_user", "web_search"} <= _tools(a), a.name
    for s in discover_skills():
        assert {"propose_sop_rule", "remember_fact"} <= _tools(built[s.agent_name]), s.name


def test_the_router_holds_no_read_tools_but_its_own(built):
    from meeseek.tools.registry import read_tool_names

    own = set(orchestrator.registry_tools())
    leaked = (set(read_tool_names(media=True)) & _tools(built[orchestrator.name()])) - own
    assert leaked == set()


def test_every_skill_gets_every_read_tool_and_the_pictures(built):
    from meeseek.tools.registry import read_tool_names

    for s in discover_skills():
        tools = [t.name for t in built[s.agent_name].tools]
        assert sorted(set(read_tool_names(media=True)) - set(tools)) == [], s.name
        assert tools.count("generate_image") == 1, s.name


def test_skill_owned_tools_and_shipping(built):
    research = _tools(built["research_agent"])
    assert {"save_refinement", "update_kb_file", "sync", "ship_output"} <= research


def test_every_write_an_agent_holds_has_an_effect(built):
    from meeseek.runtime.harness import has_effect

    for a in built.values():
        for t in a.tools:
            if t.writes:
                assert has_effect(t.name), (a.name, t.name)


def test_a_headless_turn_reads_without_pictures(monkeypatch):
    from meeseek.adapters.scripted import Scripted, say
    from meeseek.config import config

    monkeypatch.setattr(config(), "mcp_servers", [])
    plan = Scripted(say("plan"), agent="plan")
    monkeypatch.setattr(engine, "build_model", lambda name, by="": plan)
    assert engine.headless_turn("sys", "go", agent_name="plan", model="", max_turns=5, memory=False) == "plan"
    assert "kb_grep" in plan.seen[0].tools and "generate_image" not in plan.seen[0].tools
    assert "ask_user" not in plan.seen[0].tools


def test_call_caps_are_the_media_default_and_the_skills_own():
    from meeseek.config import cfg

    caps = engine._per_agent("call_limits")
    research = next(s for s in discover_skills() if s.name == "research")
    got = caps(_state(research.agent_name))
    assert got["generate_image"] == research.call_limits.get("generate_image", cfg.media.image_calls_per_turn)
    for name, n in research.call_limits.items():
        assert got[name] == n
