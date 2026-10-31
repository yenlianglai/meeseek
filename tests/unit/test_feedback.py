"""What came of a turn: your signals, recorded against the turn they are about."""

import pytest

from meeseek.context import prompts
from meeseek.runtime import feedback, inbox


@pytest.mark.parametrize("text,kind", [
    ("不對，我是說 ACME-17895", "correction"),
    ("that's not what I asked", "correction"),
    ("？", "reask"),
    ("ACME-17893 現在什麼狀態呢", "reask"),           # nearly the same request again, soon after
    ("好，那幫我開單", ""),
])
def test_the_next_message_says_whether_the_turn_landed(text, kind):
    assert feedback.implicit(text, prev_request="ACME-17893 現在什麼狀態", gap_s=60, waiting=False) == kind


def test_an_answer_to_a_question_a_slash_command_or_a_nudge_says_nothing():
    nudge = prompts.text("runtime/callbacks#empty_reply")
    assert feedback.implicit("不對", prev_request="x", gap_s=1, waiting=True) == ""
    assert feedback.implicit("/plan 不對", prev_request="x", gap_s=1, waiting=False) == ""
    assert feedback.implicit(nudge, prev_request="x", gap_s=1, waiting=False) == ""
    assert feedback.implicit("ACME-17893 現在什麼狀態", prev_request="ACME-17893 現在什麼狀態",
                             gap_s=3600, waiting=False) == ""     # the same question an hour later is a new one


def test_only_known_kinds_with_a_turn_are_recorded():
    feedback.record("i1", "s1", "thumb", value=-1, note="漏了 MR")
    feedback.record("i1", "s1", "made_up")
    feedback.record("", "s1", "thumb", value=1)
    assert [(o["kind"], o["value"], o["note"]) for o in feedback.read()] == [("thumb", -1, "漏了 MR")]


class TestInboxDecisions:
    def _held(self, key="k"):
        item = inbox.file(source="chat", key=key, title="t", meta={})
        item["session_id"] = "inbox-x"
        inbox.save(item)
        return item, inbox.hold(item, "jira_create_issue", {"summary": "A", "priority": "Low"}, "sprint_agent", "inv-7")

    def test_approving_with_edits_keeps_what_the_agent_wrote_and_the_diff(self):
        item, action = self._held()
        inbox.approve(item["id"], action["id"], {"summary": "A", "priority": "High"})
        got = inbox.get(item["id"])["actions"][0]
        assert got["proposed_args"]["priority"] == "Low" and got["args"]["priority"] == "High"
        [o] = feedback.read()
        assert o["kind"] == "write_edited" and o["inv"] == "inv-7"
        assert o["diff"] == {"priority": {"from": "Low", "to": "High"}}

    def test_approving_as_proposed_and_rejecting_with_a_note(self):
        item, action = self._held()
        inbox.approve(item["id"], action["id"], {"summary": "A", "priority": "Low"})
        item2, action2 = self._held("k2")
        inbox.reject(item2["id"], action2["id"], "不需要這張")
        assert [(o["kind"], o.get("note", "")) for o in feedback.read()] == [("write_approved", ""),
                                                                            ("write_rejected", "不需要這張")]

    def test_a_dismissal_keeps_its_reason_against_the_turn(self):
        item, _ = self._held()
        inbox.dismiss(item["id"], reason="分類錯")
        [o] = feedback.read()
        assert o["kind"] == "dismissed" and o["reason"] == "分類錯" and o["inv"] == "inv-7"


def test_an_arg_diff_names_only_what_changed():
    assert feedback.arg_diff({"a": 1, "b": "x"}, {"a": 1, "b": "y", "c": 2}) == {
        "b": {"from": "x", "to": "y"}, "c": {"from": None, "to": "2"}}
