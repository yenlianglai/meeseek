"""The core's words for a turn and a tool."""

import pytest
from pydantic import ValidationError

from meeseek.core import events as ev
from meeseek.core.tools import Effect, schema_of, tool


def _turn():
    call = ev.ToolCall(id="c1", name="jira_get_issue", args={"issue_key": "ACME-1"})
    return [ev.UserSaid(session="s", turn="t", text="ACME-1 怎麼了"),
            ev.ModelStepped(session="s", turn="t", calls=(call,)),
            ev.ToolRan(session="s", turn="t", call=call, result={"result": "In Review"}),
            ev.Replied(session="s", turn="t", text="在 Code Review")]


class TestEvents:
    def test_a_stream_round_trips_as_itself(self):
        turn = _turn()
        assert ev.load(ev.dump(turn)) == turn
        assert [e["kind"] for e in ev.dump(turn)] == ["user_said", "model_stepped", "tool_ran", "replied"]

    def test_events_are_facts(self):
        e = _turn()[0]
        with pytest.raises(ValidationError):
            e.text = "rewritten"
        with pytest.raises(ValidationError):
            ev.UserSaid(session="s", turn="t", text="x", mood="?")

    def test_a_call_cut_off_before_its_result_is_found(self):
        a, b = ev.ToolCall(id="a", name="x"), ev.ToolCall(id="b", name="jira_create_issue")
        stream = [ev.ModelStepped(session="s", turn="t", calls=(a, b)),
                  ev.ToolRan(session="s", turn="t", call=a, result="ok")]
        assert ev.calls_without_results(stream) == [b]
        held = stream + [ev.ToolHeld(session="s", turn="t", call=b, reason="waits for a yes")]
        assert ev.calls_without_results(held) == []

    def test_the_last_reply(self):
        assert ev.last_reply(_turn()) == "在 Code Review"
        assert ev.last_reply(_turn()[:2]) == ""


class TestTools:
    def test_the_schema_is_read_from_the_function(self):
        def link(issue_key: str, epic_key: str, labels: list[str] | None = None, _origin: dict | None = None) -> str:
            """Link an issue under an epic.

            Args:
                issue_key: the issue to move
                epic_key: the epic it goes under,
                    as a key
                labels: labels to add
            """
        s = schema_of(link)
        assert s["required"] == ["issue_key", "epic_key"]
        assert s["properties"]["issue_key"] == {"type": "string", "description": "the issue to move"}
        assert s["properties"]["epic_key"]["description"] == "the epic it goes under, as a key"
        assert s["properties"]["labels"]["default"] is None and "_origin" not in s["properties"]

    def test_a_write_says_what_it_changes(self):
        @tool(Effect.WRITE, target=["issue_key"])
        def link_epic(issue_key: str, epic_key: str) -> str:
            """Link an issue under an epic."""
        spec = link_epic.spec
        assert spec.writes and spec.description == "Link an issue under an epic."
        assert spec.target_of({"issue_key": "ACME-1", "epic_key": "ACME-9"}) == {"issue_key": "ACME-1"}

        with pytest.raises(TypeError, match="target"):
            @tool(Effect.WRITE)
            def comment(issue_key: str, body: str) -> str: ...
        with pytest.raises(TypeError, match="no argument"):
            @tool(Effect.WRITE, target=["key"])
            def close(issue_key: str) -> str: ...

    def test_every_registry_tool_reads_as_a_spec(self):
        from meeseek.tools import registry

        specs = {s.name: s for s in registry.RegistryTools().tools()}
        assert set(specs) == set(registry.tool_names())
        assert {n for n, s in specs.items() if s.writes} == {t.name for t in registry._ALL_TOOLS if t.writes}


def test_an_empty_reply_is_sent_back_once():
    import asyncio

    from meeseek.core.policies import EmptyReply
    from meeseek.core.turn import SendBack, TurnState

    t = TurnState(session="s", turn="t", watched=True, started=0.0, history=[])
    assert isinstance(asyncio.run(EmptyReply().on_reply(t, "  ")), SendBack)
    assert asyncio.run(EmptyReply().on_reply(t, "答案")) is None
    t.sent_back["empty_reply"] = 1
    assert asyncio.run(EmptyReply().on_reply(t, "")) is None
