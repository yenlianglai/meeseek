"""A rule or fact is written only after the person said yes (core.ledger.consented, the Consent policy)."""

import asyncio

from meeseek.core import events as ev
from meeseek.core.ledger import consented
from meeseek.core.policies import Consent
from meeseek.core.tools import ToolSpec
from meeseek.core.turn import Hold, TurnState

Q = ev.ToolCall(id="q", name="ask_user", args={})


def ask(turn="i1"):
    return [ev.ModelStepped(session="s", turn=turn, calls=(Q,)),
            ev.ToolRan(session="s", turn=turn, call=Q, result={"status": "waiting for the user's answer"})]


def answer(text, turn="i1"):
    return [ev.UserSaid(session="s", turn=turn, text=text, answers="q")]


def said(text, turn):
    return [ev.UserSaid(session="s", turn=turn, text=text)]


def test_no_question_no_write():
    assert not consented(said("以後開單都加一行", "i1"), "i1")


def test_a_question_still_waiting_is_not_a_yes():
    assert not consented(ask(), "i1")


def test_a_yes_to_the_question_lets_it_through():
    assert consented(ask("i1") + answer("寫入", "i2"), "i2")


def test_a_refusal_does_not():
    assert not consented(ask("i1") + answer("不要", "i2"), "i2")


def test_a_yes_in_the_next_message_counts():
    assert consented(ask("i1") + said("同意，寫進規則", "i2"), "i2")


def test_an_old_question_does_not_cover_a_later_write():
    events = ask("i1") + answer("寫入", "i2") + said("再問一件事", "i3") + said("還有", "i4")
    assert not consented(events, "i4")


def test_the_user_asking_for_it_is_the_consent():
    assert consented(said("把這條寫進 code skill 的學習規則，我同意寫入：…", "i1"), "i1")
    assert consented(said("記住：prod 的 namespace 是 acme-app", "i1"), "i1")
    assert not consented(said("以後開單都加一行「建立者」", "i1"), "i1"), "a correction is not a request to record"
    assert not consented(said("先不要寫進規則", "i1"), "i1")


def _state(events, turn="i1"):
    return TurnState(session="s", turn=turn, watched=True, started=0.0, history=[], events=events)


def _spec(name):
    return ToolSpec(name=name, description="d", fn=lambda **k: "", parameters={"type": "object", "properties": {}})


def test_the_policy_holds_the_write_and_says_what_to_do():
    policy = Consent({"remember_fact": "這筆記憶"})
    t = _state(said("prod 的 namespace 是 acme-app", "i1"))
    held = asyncio.run(policy.on_tool(t, ev.ToolCall(id="c", name="remember_fact"), _spec("remember_fact")))
    assert isinstance(held, Hold) and not held.queued and "ask_user" in held.reason
    assert asyncio.run(policy.on_tool(t, ev.ToolCall(id="c", name="kb_grep"), _spec("kb_grep"))) is None


def test_an_approval_is_consent():
    policy = Consent({"remember_fact": "這筆記憶"}, approved=lambda t, c: c.name == "remember_fact")
    t = _state(said("記一下", "i1"))
    assert asyncio.run(policy.on_tool(t, ev.ToolCall(id="c", name="remember_fact"), _spec("remember_fact"))) is None
