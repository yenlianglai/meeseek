"""What a tool's result looks like to the model (runtime/results.py, the engine's ResultCap and ArgCap).

A result is marked failed only from its leading text: a successful result whose
content mentions "failed" or "not found" must stay unmarked.
"""

import asyncio
import json

from meeseek.adapters.memory import MemoryEventStore
from meeseek.adapters.scripted import Scripted, call, say
from meeseek.core.events import ToolCall, ToolRan
from meeseek.core.tools import tool
from meeseek.core.turn import Run, TurnRunner, TurnState
from meeseek.engine import ArgCap, ResultCap
from meeseek.runtime.results import _MAX_TOOL_RESULT_CHARS, _is_error, cap_result, slim_mcp, tool_failed


class TestErrorClassification:
    def test_leading_error_prefix_is_error(self):
        assert _is_error("Error: MR 73 not found")
        assert _is_error("Failed to connect to GitLab")

    def test_content_mentioning_failure_is_not_error(self):
        doc = ("### deploy_runbook.md (score: 12.3)\nIf the pipeline failed, check the not-found errors in the logs "
               "and the exception traceback.")
        assert not _is_error(doc)

    def test_successful_sync_is_not_error(self):
        assert not _is_error("Sync: 0 updated, 3 skipped (all up to date)")


class TestResultCap:
    def test_small_result_untouched(self):
        assert cap_result("short") is None and cap_result({"result": "ok"}) is None

    def test_oversized_result_truncated_with_head_and_guidance(self):
        big = "ERROR boom\n" + ("x" * (_MAX_TOOL_RESULT_CHARS + 10_000))
        out = cap_result(big)
        assert out["result"].startswith("ERROR boom") and "truncated" in out["result"]
        assert len(out["result"]) < _MAX_TOOL_RESULT_CHARS + 500


class TestSlimMcpResult:
    """Every later step re-sends a result, so what only repeats or decorates is dropped once."""

    def _shape(self, result):
        t = TurnState(session="s", turn="t", watched=True, started=0.0, history=[])
        return ResultCap().on_result(t, ToolCall(id="c", name="jira_search"), None, result)

    def test_the_duplicate_avatars_and_indentation_go_the_data_stays(self):
        person = {"account_id": "712020:a4", "display_name": "Robin", "email": "r@x", "avatar_url": "https://x/" + "a" * 150}
        payload = {"issues": [{"key": "ACME-1", "summary": "交付 API", "assignee": person, "reporter": person}]}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        out = self._shape({"meta": {}, "content": [{"type": "text", "text": text}],
                           "structuredContent": {"result": text}, "isError": False})
        assert "structuredContent" not in out and out["isError"] is False
        slim = out["content"][0]["text"]
        assert len(slim) < len(text) / 2
        issue = json.loads(slim)["issues"][0]
        assert issue["assignee"] == {"account_id": "712020:a4", "display_name": "Robin", "email": "r@x"}
        assert issue["summary"] == "交付 API"

    def test_text_that_is_not_json_is_kept_as_it_came(self):
        out = self._shape({"content": [{"type": "text", "text": "plain words"}], "structuredContent": {"result": "plain words"}})
        assert out["content"] == [{"type": "text", "text": "plain words"}] and "structuredContent" not in out

    def test_a_result_with_no_text_is_untouched(self):
        assert slim_mcp({"content": [{"type": "image", "data": "..."}], "structuredContent": {"x": 1}}) is None
        assert slim_mcp({"result": "a function tool's output"}) is None


class TestToolFailedMarker:
    def _ran(self, returns):
        @tool()
        def get_file(path: str) -> str:
            """Read a file."""
            return returns

        store = MemoryEventStore()
        asyncio.run(TurnRunner(model=Scripted(call("c", "get_file", path="x"), say("ok")), tools=[get_file.spec],
                               store=store).run("s", "read it"))
        return next(e for e in store.read("s") if isinstance(e, ToolRan))

    def test_a_result_that_says_error_is_failed(self):
        assert self._ran("Error: GitLab API error: HTTP 404").failed

    def test_content_mentioning_error_is_not_marked(self):
        assert not self._ran("def handle_error(): ... raises on failed login").failed

    def test_the_failure_marker_is_one_definition(self):
        assert tool_failed({"tool_failed": True}) and tool_failed({"error": "x"})
        assert not tool_failed({"error": "x", "result": "y"}) and not tool_failed("Error: text")


class TestArgCap:
    """A skill's `limits:` clamp row and line counts at the query (the infra skill's, here)."""

    CAPS = {"list_log_entries": {"pageSize": 50}, "get_k8s_logs": {"tail": "200"}}

    def _cap(self, name, args):
        t = TurnState(session="s", turn="t", watched=True, started=0.0, history=[])
        out = asyncio.run(ArgCap(lambda t: self.CAPS).on_tool(t, ToolCall(id="c", name=name, args=args), None))
        return out.args if isinstance(out, Run) else args

    def test_sets_limit_when_omitted(self):
        assert self._cap("list_log_entries", {"filter": "x"})["pageSize"] == 50
        assert self._cap("get_k8s_logs", {"name": "pod"})["tail"] == "200"  # string kept

    def test_clamps_when_too_high(self):
        assert self._cap("list_log_entries", {"pageSize": 5000})["pageSize"] == 50
        assert self._cap("get_k8s_logs", {"tail": "9999"})["tail"] == "200"

    def test_keeps_when_under_cap(self):
        assert self._cap("list_log_entries", {"pageSize": 20})["pageSize"] == 20

    def test_uncapped_tool_untouched(self):
        assert self._cap("kb_grep", {"pattern": "x"}) == {"pattern": "x"}
