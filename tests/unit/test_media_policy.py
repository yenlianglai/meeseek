"""Pictures are part of an answer — and the prompts must not say otherwise.

`generate_image` reaches the agents that talk to people (not headless workflows
or delegated sub-tasks), under a per-turn cap; no prompt says drawing must be
`render_diagram` or calls another tool name a hallucination.
"""

import re

import pytest

from meeseek.config import PROMPTS_DIR, cfg, home
from meeseek.runtime.skills import discover_skills
from meeseek import engine

MEDIA_HEADING = "## 視覺產出"


def _tool_names(agent) -> set[str]:
    return {t.name for t in agent.tools}


class TestConversationalAgentsCanDraw:
    def test_every_skill_agent_holds_both_picture_tools(self):
        for skill in discover_skills():
            names = _tool_names(engine.skill_agent(skill))
            assert {"generate_image", "render_diagram"} <= names, skill.name

    def test_the_guidance_travels_with_the_tool(self):
        """When to draw is in the picture tools' own descriptions, so it reaches
        exactly the agents that hold them — and the router, which holds neither,
        is not told."""
        from meeseek.runtime.orchestrator import instruction
        from meeseek.tools.registry import _ALL_TOOLS

        def describe(name):
            return next(t.description for t in _ALL_TOOLS if t.name == name)

        assert "STRUCTURE" in describe("render_diagram")
        assert "APPEARANCE" in describe("generate_image") and "reference_images" in describe("generate_image")
        assert "generate_image" not in instruction()


class TestHeadlessPathsDoNotDraw:
    def test_read_tools_drops_the_picture_tools_when_media_is_off(self):
        from meeseek.tools.registry import CLAUDE_TOOLS, MEDIA_TOOLS, read_tool_names

        with_media = set(read_tool_names(media=True))
        without = set(read_tool_names(media=False))
        assert set(MEDIA_TOOLS) <= with_media
        assert without == with_media - set(MEDIA_TOOLS) - set(CLAUDE_TOOLS)


class TestNoPromptForbidsDrawing:
    PROMPTS = (sorted(p for p in PROMPTS_DIR.rglob("*.md") if p.name != "README.md")
               + sorted(home().skills.glob("*/SKILL.md")))

    @pytest.mark.parametrize("path", PROMPTS, ids=lambda p: p.parent.name + "/" + p.name)
    def test_no_prompt_says_drawing_must_be_render_diagram(self, path):
        """Such a rule turns every appearance request ("draw me what this NPC
        looks like") into a diagram request."""
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"畫圖.{0,8}必須.{0,12}render_diagram", text), path

    @pytest.mark.parametrize("path", PROMPTS, ids=lambda p: p.parent.name + "/" + p.name)
    def test_no_prompt_defines_other_tool_names_as_hallucinations(self, path):
        """Calling every tool name but render_diagram invented would make
        generate_image a hallucination."""
        text = path.read_text(encoding="utf-8")
        assert "render_diagram` 以外的名字" not in text, path

    def test_the_router_routes_visual_intent_and_never_refuses(self):
        from meeseek.runtime.orchestrator import instruction

        router = instruction()                                     # as the router gets it: the skills' routes in
        common = (PROMPTS_DIR / "blocks" / "common.md").read_text(encoding="utf-8")
        assert "要看圖" in router and "→ research_agent" in router   # picture requests are routed
        assert "generate_image" not in router                      # the router does not draw
        assert "不要說你沒有某個能力" in common                       # nobody claims it cannot


class TestCallBudget:
    def test_the_per_turn_cap_comes_from_config(self):
        from meeseek.runtime.media import media_call_limits

        assert media_call_limits() == {"generate_image": cfg.media.image_calls_per_turn}

    def _caps(self, skill_name: str) -> dict:
        """The caps the engine's call budget applies while this skill's agent answers."""
        from meeseek.core.turn import TurnState
        from meeseek.runtime.skills import Skill

        return engine._per_agent("call_limits")(TurnState(session="s", turn="t", watched=True, started=0.0,
                                                          history=[], agent=Skill._to_agent_name(skill_name)))

    def test_every_skill_agent_carries_the_image_cap_unless_it_sets_its_own(self):
        """Advisory, not a hard stop: past the cap a call is told to converge and
        only twice it is blocked, so a storyboard still works while a runaway
        loop stops. A skill's own limit wins: the media cap is a default."""
        for skill in discover_skills():
            want = skill.call_limits.get("generate_image", cfg.media.image_calls_per_turn)
            assert self._caps(skill.name).get("generate_image") == want, skill.name

    def test_a_skills_own_limit_wins(self, monkeypatch):
        from meeseek.runtime import skills as sk

        other = cfg.media.image_calls_per_turn + 7
        real = sk.discover_skills()
        import dataclasses

        tweaked = [s if s.name != "research" else dataclasses.replace(s, call_limits={"generate_image": other})
                   for s in real]
        monkeypatch.setattr(sk, "discover_skills", lambda skills_dir=None: tweaked)
        assert self._caps("research")["generate_image"] == other
