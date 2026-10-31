"""The raw output a turn fetched reaches the reply even when the model leaves it out (core.ledger.raw_block)."""

import asyncio

from meeseek.core import events as ev
from meeseek.core.ledger import raw_block
from meeseek.core.policies import Evidence
from meeseek.core.turn import Annotate, TurnState

LOGS = "### uat\n```text\n2026-10-09 01:00:00 ERROR pod | boom\n```\nfilter:\n```\nx\n```"
RAW = {"query_logs", "k8s_status"}.__contains__


def _result(name, result, turn="i1"):
    return ev.ToolRan(session="s", turn=turn, call=ev.ToolCall(id=name, name=name), result={"result": result})


def _reply(events, text):
    t = TurnState(session="s", turn="i1", watched=True, started=0.0, history=[], events=events)
    return asyncio.run(Evidence(RAW).on_reply(t, text))


def test_the_first_block_of_the_last_raw_result_goes_in_front():
    out = _reply([_result("query_logs", LOGS)], "uat 有一筆 ERROR。")
    assert isinstance(out, Annotate) and out.before
    assert out.note == "```text\n2026-10-09 01:00:00 ERROR pod | boom\n```"


def test_a_reply_that_already_shows_a_block_is_left_alone():
    assert _reply([_result("query_logs", LOGS)], "```\nmine\n```") is None


def test_other_tools_and_earlier_turns_do_not_count():
    assert _reply([_result("kb_grep", LOGS)], "答案") is None
    assert _reply([_result("query_logs", LOGS, turn="i0")], "答案") is None


def test_a_long_block_is_trimmed():
    rows = "\n".join(f"line {i}" for i in range(100))
    block = raw_block([_result("k8s_status", f"```text\n{rows}\n```")], "i1", RAW)
    assert len(block.splitlines()) == 28 and "more lines" in block and block.endswith("```")
