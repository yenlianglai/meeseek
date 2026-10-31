"""Tool registry: tool names are consistent and unique."""

import asyncio

from meeseek import engine
from meeseek.tools.registry import GATED_TOOLS, GENERAL_TOOLS, MEDIA_TOOLS, _ALL_TOOLS, read_tool_names, tool_names


def _all_tools():
    """Every registered tool, as the engine holds it."""
    return [engine.registry_spec(n) for n in tool_names()]


class TestToolNameConsistency:
    def test_every_tool_reaches_the_engine_under_its_name(self):
        specs = _all_tools()
        assert {t.name for t in specs} == {td.name for td in _ALL_TOOLS}
        assert all(t.name.isidentifier() for t in specs)

    def test_tool_names_are_globally_unique(self):
        names = [td.name for td in _ALL_TOOLS]
        dupes = {n for n in names if names.count(n) > 1}
        assert dupes == set(), f"duplicate tool name(s): {sorted(dupes)}"
        assert tool_names() == sorted(names)


class TestToolFunctionality:
    def test_every_tool_is_described_with_a_schema(self):
        for t in _all_tools():
            assert t.description.strip(), t.name
            assert t.parameters.get("type") == "object", t.name
            assert t.parameters.get("properties") or t.name in ("list_claude_sessions",), t.name

    def test_the_engine_knows_which_write(self):
        assert {t.name for t in _all_tools() if t.writes} == set(GATED_TOOLS)


class TestWritesIsTheOnlyPolicyAxis:
    """`writes` decides who may hold a tool. There is no second axis."""

    def test_read_tools_is_exactly_the_non_writing_tools(self):
        assert set(read_tool_names(media=True)) == set(GENERAL_TOOLS)
        assert set(GENERAL_TOOLS) == {td.name for td in _ALL_TOOLS if not td.writes}
        assert set(GATED_TOOLS) == {td.name for td in _ALL_TOOLS if td.writes}
        assert set(GENERAL_TOOLS).isdisjoint(GATED_TOOLS)
        assert set(GENERAL_TOOLS) | set(GATED_TOOLS) == set(tool_names())

    def test_headless_runs_can_opt_out_of_the_picture_tools(self):
        """Their output is a summary nobody views — see MEDIA_TOOLS."""
        from meeseek.tools.registry import CLAUDE_TOOLS

        lean = set(read_tool_names(media=False))
        assert lean == set(GENERAL_TOOLS) - set(MEDIA_TOOLS) - set(CLAUDE_TOOLS)
        assert "generate_image" not in lean
        assert "kb_grep" in lean


class TestUserImagesPassthrough:
    """A tool that draws from the person's pictures gets the ones this turn came with."""

    def _spec(self, monkeypatch, seen):
        from meeseek.tools import registry

        def fake_fn(prompt, _attached_images=None):
            seen["attached"] = _attached_images
            return "ok"

        td = registry._ToolDef(
            name="fake_img", description="d", fn=fake_fn,
            parameters={"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
            wants_user_images=True)
        monkeypatch.setitem(registry._INDEX, "fake_img", td)
        return engine.registry_spec("fake_img")

    def test_the_turns_pictures_reach_the_tool(self, monkeypatch):

        from meeseek.adapters.memory import MemoryEventStore
        from meeseek.adapters.scripted import Scripted, call, say
        from meeseek.core.turn import TurnRunner

        seen = {}
        spec = self._spec(monkeypatch, seen)
        runner = TurnRunner(model=Scripted(call("c", "fake_img", prompt="x"), say("done")), tools=[spec],
                            store=MemoryEventStore())
        asyncio.run(_run(runner))
        assert seen["attached"] == [("image/png", b"png")]

    def test_outside_a_turn_there_are_none(self, monkeypatch):
        seen = {}
        self._spec(monkeypatch, seen).fn(prompt="x")
        assert seen["attached"] == []


async def _run(runner):
    from meeseek.adapters.blobs import blobs
    from meeseek.core.events import Attachment

    files = [Attachment(mime="image/png", ref=blobs().put(b"png")), Attachment(mime="application/pdf", ref=blobs().put(b"pdf"))]
    return await runner.run("s", "draw from these", attachments=files)
