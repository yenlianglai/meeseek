"""Slash command routing: deterministic dispatch.

Tests iterate COMMANDS rather than naming commands: the dispatcher, palette and
router prompt all derive from that table, and a hard-coded list would be a
second source of truth.
"""

from unittest.mock import patch

import pytest

from meeseek.runtime.slash_commands import COMMANDS, SlashCommand, _SLASH_RE, slash_command_list, slash_command_tables

_RUNS = [c for c in COMMANDS if c.run is not None]
_SKILLS = [c for c in COMMANDS if c.skill]
_FEATURED = [c for c in COMMANDS if c.featured]


class TestCommandsIsTheOnlyTable:
    def test_every_command_does_exactly_one_thing(self):
        """`run` XOR `skill` — enforced in __post_init__, so a command cannot be
        both a local call and a transfer, nor neither."""
        for c in COMMANDS:
            assert bool(c.run) != bool(c.skill), c.name
        with pytest.raises(ValueError):
            SlashCommand("bad", "", "neither run nor skill")
        with pytest.raises(ValueError):
            SlashCommand("bad", "", "both", run=lambda: "", skill="code")

    def test_names_are_unique(self):
        names = [c.name for c in COMMANDS]
        assert len(names) == len(set(names))

    def test_every_skill_target_is_an_installed_skill(self):
        """A transfer to a skill that does not exist produces an agent that
        cannot be reached."""
        from meeseek.runtime.skills import discover_skills

        installed = {s.name for s in discover_skills()}
        for c in _SKILLS:
            assert c.skill in installed, f"/{c.name} → unknown skill {c.skill!r}"

    def test_an_unknown_command_falls_through_to_the_model(self):
        from meeseek.runtime.slash_commands import _BY_NAME

        assert _BY_NAME.get(_SLASH_RE.match("/no-such-command x").group(1)) is None


class TestSlashCommandParsing:
    def test_every_command_is_recognised(self):
        for c in COMMANDS:
            match = _SLASH_RE.match(f"/{c.name} some args")
            assert match, f"/{c.name} did not match slash regex"
            assert match.group(1).lower() == c.name
            assert match.group(2).strip() == "some args"

    def test_unknown_command_returns_none(self):
        from meeseek.runtime.slash_commands import _BY_NAME

        assert _BY_NAME.get(_SLASH_RE.match("/unknown_xyz").group(1)) is None

    def test_non_slash_message_not_matched(self):
        assert _SLASH_RE.match("hello world") is None


class TestSlashCommandDispatch:
    """The engine's dispatch (engine._slash): a command that runs here answers
    without a model; one that names a skill starts the turn with its agent."""

    def _slash(self, text):
        import asyncio

        from meeseek import engine

        return asyncio.run(engine._slash("web-slash", text, "webui", None))

    def test_run_command_executes_and_completes_the_turn(self):
        with patch("meeseek.context.sync.sync", return_value="done") as run:
            done, start_with = self._slash("/sync")
        run.assert_called_once_with()
        assert done is not None and done.reply == "done" and start_with == ""

    def test_skill_command_starts_with_its_agent(self):
        assert self._slash("/research ideas for new feature") == (None, "research_agent")

    def test_refine_reaches_research_now_that_knowledge_is_merged(self):
        assert self._slash("/refine the emotion doc") == (None, "research_agent")

    def test_anything_else_goes_to_the_model(self):
        assert self._slash("hello") == (None, "") and self._slash("/nope x") == (None, "")


class TestSlashCommandList:
    """The palette payload the console renders straight from."""

    def test_long_running_follows_the_kind_of_command(self):
        rows = {c["name"]: c for c in slash_command_list()}
        for c in _RUNS:
            assert rows[c.name]["long_running"] is True, f"/{c.name} runs inline"
        for c in _SKILLS:
            assert rows[c.name]["long_running"] is False, f"/{c.name} streams"

    def test_featured_commands_come_first_in_declared_order(self):
        names = [c["name"] for c in slash_command_list()]
        featured = [c.name for c in _FEATURED]
        assert names[: len(featured)] == featured
        assert [c["name"] for c in slash_command_list() if c["featured"]] == featured

    def test_remaining_commands_stay_sorted_by_name(self):
        rest = [c["name"] for c in slash_command_list()[len(_FEATURED):]]
        assert rest == sorted(rest)

    def test_every_command_appears_exactly_once(self):
        listed = [c["name"] for c in slash_command_list()]
        assert sorted(listed) == sorted(c.name for c in COMMANDS)


class TestRouterTables:
    def test_tables_are_generated_from_the_same_table(self):
        """Both prompt tables render from COMMANDS, so a listed target is the
        one the dispatcher calls."""
        tool_table, agent_table = slash_command_tables()
        for c in _RUNS:
            assert f"`/{c.name}`" in tool_table and f"`{c.target}()`" in tool_table
        for c in _SKILLS:
            assert f"`/{c.name}" in agent_table and c.target in agent_table
        for c in _SKILLS:
            assert f"`/{c.name}" not in tool_table
