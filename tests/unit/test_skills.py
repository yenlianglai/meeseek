"""The skill loader: skills define SOPs, tools attach to skills."""


from meeseek.runtime.skills import discover_skills
from meeseek import engine

# Exactly these, not "at least these": a skill is a routing target, a prompt and
# a write grant, so an extra one is a fork of someone's job and a missing one is
# a dangling handoff.
EXPECTED_SKILLS = {"research", "infra", "sprint", "code"}


class TestDiscovery:
    def test_exactly_these_skills_are_installed(self):
        assert {s.name for s in discover_skills()} == EXPECTED_SKILLS

    def test_every_agent_skill_has_body(self):
        """Agent skills need a non-empty SOP body in SKILL.md."""
        for skill in discover_skills():
            assert skill.body.strip(), f"{skill.name} has empty SKILL.md body"

    def test_every_skill_says_when_it_is_done(self):
        for skill in discover_skills():
            assert "## 完成標準" in skill.body, f"{skill.name} has no 完成標準 section"

    def test_tools_frontmatter_holds_only_write_tools(self):
        from meeseek.tools.registry import read_tool_names

        reads = set(read_tool_names(media=True))
        for skill in discover_skills():
            assert not (set(skill.tools) & reads), (
                f"{skill.name}: `tools:` lists read tools — they attach anyway")

    def test_a_read_tool_in_tools_is_a_startup_error(self, tmp_path):
        """Only a deliberately bad skill proves the loader still refuses: `tools:`
        is a write grant and must not double as decoration for reads."""
        import pytest

        import meeseek.runtime.skills as sk

        d = tmp_path / "demo"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\ndescription: d\ntools: comment_mr, kb_grep\n---\nbody")
        with pytest.raises(ValueError, match="kb_grep"):
            sk._parse_skill(d)

    def test_unknown_frontmatter_keys_are_rejected(self, tmp_path):
        """Silently dropping a key is how a SKILL.md comes to say one thing while
        the runtime does another. `name:` is rejected too: the directory is the
        identity."""
        import pytest

        import meeseek.runtime.skills as sk

        for bad in ("name: other", "api_key_env: FOO", "iteration_budget: 5"):
            d = tmp_path / f"demo_{bad.split(':')[0]}"
            d.mkdir()
            (d / "SKILL.md").write_text(f"---\ndescription: d\n{bad}\n---\nbody")
            with pytest.raises(ValueError, match="unknown frontmatter key"):
                sk._parse_skill(d)

    def test_skill_owned_tools_attach_without_being_declared(self):
        """Owning the tool IS the grant — skills/<name>/tools.py needs no
        frontmatter entry."""
        skills = {s.name: s for s in discover_skills()}
        assert "update_kb_file" not in skills["research"].tools
        research = [t.name for t in engine.skill_agent(skills["research"]).tools]
        assert {"save_refinement", "update_kb_file", "sync"} <= set(research)


    def test_descriptions_are_rendered(self):
        """{{placeholders}} in SKILL.md descriptions must be resolved."""
        for skill in discover_skills():
            assert skill.description, f"{skill.name} has empty description"

    def test_tool_names_resolve(self):
        """Every declared tool name must bind — typos fail here, not at runtime.

        Building the skill's agent binds every name it declares."""
        for skill in discover_skills():
            engine.skill_agent(skill)  # raises KeyError on an unknown name


class TestRoutingTable:
    """The router's tables are generated — SKILL.md is the single source of truth."""

    def test_descriptions_are_rendered(self):
        """The router reads each skill's description; the core's placeholders are
        filled (a pack's fill in once it runs: packs/tests checks those)."""
        from meeseek.context.prompts import _get_placeholder_values

        core = set(_get_placeholder_values())
        for skill in discover_skills():
            assert "Keywords:" in skill.description, skill.name
            assert not any("{{" + k + "}}" in skill.description for k in core), skill.name

    def test_a_running_packs_placeholders_fill_a_description(self):
        from meeseek.app import build_app

        class Running:
            def placeholders(self):
                return {"jira_project_key": "ACME"}

        sprint = next(s for s in discover_skills() if "{{jira_project_key}}" in s.raw_description)
        a = build_app()
        a.mcp_tools = Running()
        with a.active():
            assert "{{" not in sprint.description and "ACME-" in sprint.description

    def test_the_routers_prompt_names_no_skill_and_each_skills_routes_reach_it(self):
        """Who gets what is the skills' to say (SKILL.md `routes:`): the
        engine's prompt works with any set of skills."""
        from meeseek.config import PROMPTS_DIR
        from meeseek.runtime.orchestrator import instruction

        raw = (PROMPTS_DIR / "agents" / "router.md").read_text(encoding="utf-8")
        skills = discover_skills()
        assert not [s.agent_name for s in skills if s.agent_name in raw]
        text = instruction()
        for s in skills:
            for r in s.routes:
                assert f"- {r} → {s.agent_name}" in text

    def test_router_instruction_embeds_generated_tables(self):
        from meeseek.runtime.orchestrator import instruction as _instruction

        instruction = _instruction()
        from meeseek.runtime.slash_commands import COMMANDS

        for cmd in COMMANDS:
            assert f"`/{cmd.name}" in instruction


class TestBuildAgent:
    def test_skill_names_are_kebab_case(self):
        """ADK's skill format (and IDE validators) require lowercase kebab-case
        names. The directory IS the name — a `name:` in the frontmatter is a
        startup error, because two spellings of one identity could disagree."""
        import re

        kebab = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
        for skill in discover_skills():
            assert kebab.match(skill.name), f"skill name not kebab-case: {skill.name!r}"
            assert "\nname:" not in (skill.path / "SKILL.md").read_text(encoding="utf-8")

    def test_the_ticket_format_sop_has_exactly_one_home(self):
        """The router does not open tickets; the sprint agent's SOP holds the format."""
        from meeseek.config.paths import home

        from meeseek.runtime.orchestrator import instruction

        router = instruction()
        sprint = (home().skills / "sprint" / "SKILL.md").read_text(encoding="utf-8")
        assert "具體改動範疇" in sprint
        assert "具體改動範疇" not in router
        assert "開單格式在它的 SOP 裡" in router  # router points at it instead (the sprint skill's route)

class TestSkillStructurePolicy:
    """skills/<name>/ 只允許 SKILL.md（必要）、tools.py（選配）、learned.md（runtime）。"""

    ALLOWED_FILES = {"SKILL.md", "tools.py", "learned.md"}
    ALLOWED_DIRS = {"__pycache__"}

    def test_every_skill_follows_canonical_layout(self):
        from meeseek.config import home

        for skill_dir in sorted(home().skills.iterdir()):
            if not skill_dir.is_dir():
                continue
            assert (skill_dir / "SKILL.md").exists(), f"{skill_dir.name}: missing SKILL.md"
            for item in skill_dir.iterdir():
                if item.is_dir():
                    assert item.name in self.ALLOWED_DIRS, (
                        f"{skill_dir.name}/{item.name}/: unexpected dir — "
                        "skills 只有固定三檔：SKILL.md / tools.py / learned.md"
                    )
                else:
                    assert item.name in self.ALLOWED_FILES, (
                        f"{skill_dir.name}/{item.name}: unexpected file — "
                        "skills 只有固定三檔：SKILL.md / tools.py / learned.md"
                    )


class TestModelResolution:
    """model: 欄位——tier 別名走 project.yaml models:，其他值直接當 model id；含 '/' 的走 litellm。"""

    def _skill(self, tmp_path, model_line):
        import meeseek.runtime.skills as sk
        d = tmp_path / "demo"
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\ndescription: d\nmodel: {model_line}\n---\nbody")
        return sk._parse_skill(d)

    def _model(self, tmp_path, model_line):
        return engine.skill_agent(self._skill(tmp_path, model_line)).model.inner

    def test_tier_alias(self, tmp_path):
        from meeseek.config import cfg

        assert self._model(tmp_path, "fast").model == cfg.models.fast

    def test_literal_model_id_passthrough(self, tmp_path):
        assert self._model(tmp_path, "gemini-2.5-pro").model == "gemini-2.5-pro"

    def test_bedrock_model_uses_litellm(self, tmp_path, monkeypatch):
        from meeseek.adapters import llm
        from meeseek.adapters.llm import LitellmModel

        monkeypatch.setattr(llm, "_installed", lambda m: True)
        model = self._model(tmp_path, "bedrock/anthropic.claude-sonnet-4-5-v1:0")
        assert isinstance(model, LitellmModel) and model.model == "bedrock/anthropic.claude-sonnet-4-5-v1:0"

    def test_gemini_stays_on_gemini(self, tmp_path):
        from meeseek.adapters.llm import GenaiModel

        assert isinstance(self._model(tmp_path, "fast"), GenaiModel)


class TestGeneralTools:
    def test_the_picture_tools_say_which_is_which(self):
        from meeseek.tools.registry import _ALL_TOOLS

        def describe(name):
            return next(t.description for t in _ALL_TOOLS if t.name == name)

        assert "STRUCTURE" in describe("render_diagram") and "generate_image" in describe("render_diagram")
        assert "APPEARANCE" in describe("generate_image") and "render_diagram" in describe("generate_image")
