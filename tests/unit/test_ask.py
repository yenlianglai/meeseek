"""ask_user: the open question is read from the conversation (history.pending_question), and the next message answers it."""

from meeseek.runtime import history
from meeseek.runtime.history import Row


def _row(author, parts, turn="inv-1"):
    return Row(user="webui", session="s", turn=turn, at=0.0, event={"author": author, "content": {"parts": parts}})


def _ask(cid="q1"):
    return _row("code_agent", [{"function_call": {"id": cid, "name": "ask_user", "args": {
        "question": "要跑哪個測試？", "options": ["unit", "e2e"], "recommended": "unit"}}}])


def _tool_ack(cid="q1"):
    # the tool's own interim response: the call is still waiting for the person
    return _row("code_agent", [{"function_response": {"id": cid, "name": "ask_user", "response": {"status": "waiting"}}}])


def _user_answer(cid="q1"):
    return _row("user", [{"function_response": {"id": cid, "name": "ask_user", "response": {"answer": "unit"}}}])


def _user_text(text, turn="inv-2"):
    return _row("user", [{"text": text}], turn=turn)


def test_an_unanswered_call_is_pending():
    q = history.pending_question([_ask(), _tool_ack()])
    assert q.call_id == "q1" and q.options == ["unit", "e2e"] and q.recommended == "unit"
    assert "建議：unit" in q.as_text()


def test_an_answered_call_is_not():
    assert history.pending_question([_ask(), _tool_ack(), _user_answer()]) is None


def test_a_new_user_message_closes_it():
    assert history.pending_question([_ask(), _tool_ack(), _user_text("算了，先看別的")]) is None


def test_the_turn_is_waiting():
    assert history.turn_status([_ask(), _tool_ack()], "inv-1") == "waiting"
    assert history.turn_status([_user_text("hi", "inv-1"), _row("code_agent", [{"text": "好"}])], "inv-1") == "done"
    assert history.turn_status([], "inv-9") == "unknown"
