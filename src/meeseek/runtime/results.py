"""What a tool's result looks like to the model.

  * slim_mcp     an MCP result without what only repeats or decorates
  * cap_result   anything past the cap cut to its head, with a note saying so
  * tool_failed  whether a result reports a failure — one definition, shared
                 by the engine's result cap, the Inbox and the MCP progress view
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from meeseek.context import prompts

logger = logging.getLogger(__name__)

# Cap on one tool result fed back to the model (~12K tokens). Registry tools
# self-truncate; remote MCP results (log dumps, jira_search: ~80-110K chars)
# arrive whole and would otherwise crowd out the 1M-token input limit.
_MAX_TOOL_RESULT_CHARS = 50_000


def tool_failed(response) -> bool:
    """Did this tool response report a failure? Shared with the MCP progress
    view so the two never disagree."""
    if not isinstance(response, dict):
        return False
    if response.get("tool_failed"):
        return True
    return "error" in response and "result" not in response


# Keys no model reads: pictures of people (a long URL per person, twice an issue).
_NOISE_KEYS = frozenset({"avatar_url", "avatarUrls"})


def _drop_noise(value):
    if isinstance(value, dict):
        return {k: _drop_noise(v) for k, v in value.items() if k not in _NOISE_KEYS}
    if isinstance(value, list):
        return [_drop_noise(v) for v in value]
    return value


def slim_mcp(result) -> Optional[dict]:
    """An MCP result without what only repeats or decorates, else None.

    Every step of a turn re-sends every earlier result, so this is paid many
    times: an MCP result carries its payload twice (`content` text and
    `structuredContent`), as indented JSON, with an avatar URL per person —
    a jira_search of 10 issues was ~46K chars, ~17K without. The text in
    `content` is kept, minified when it is JSON, and the copy dropped.
    """
    if not (isinstance(result, dict) and isinstance(result.get("content"), list)):
        return None
    texts = [c for c in result["content"] if isinstance(c, dict) and c.get("type") == "text"]
    if not texts:
        return None
    content = []
    for c in result["content"]:
        if c in texts:
            try:
                c = {**c, "text": json.dumps(_drop_noise(json.loads(c["text"])),
                                             ensure_ascii=False, separators=(",", ":"))}
            except (ValueError, TypeError):
                pass  # not JSON: kept as it came
        content.append(c)
    return {**{k: v for k, v in result.items() if k != "structuredContent"}, "content": content}


def cap_result(result) -> Optional[dict]:
    """A `{"result": str}` replacement keeping the head if `result` is
    oversized, else None. Errors and the most relevant rows are usually at the
    top of log and metric dumps."""
    text = str(result) if result else ""
    if len(text) <= _MAX_TOOL_RESULT_CHARS:
        return None
    head = text[:_MAX_TOOL_RESULT_CHARS]
    note = prompts.text("runtime/callbacks#truncated",
                        chars=f"{len(text):,}", cap=f"{_MAX_TOOL_RESULT_CHARS:,}")
    return {"result": f"{head}\n\n{note}"}


def _payload_text(result) -> str:
    """The text a tool actually returned, unwrapped from its result dict."""
    if result is None:
        return ""
    if isinstance(result, dict):
        if "result" in result:
            return str(result["result"] or "")
        if "error" in result:
            return "error: " + str(result["error"] or "")
    return str(result)


def _is_error(result_str: str) -> bool:
    """Heuristic: does the result *start* like an error? Tools here signal
    failure with a leading "Error:" / "Failed"; matching only the prefix avoids
    flagging content that merely mentions "failed" (a KB doc, an MR diff)."""
    head = result_str.lstrip().lower()[:60]
    return any(head.startswith(marker) for marker in [
        "error:", "error ", "failed", "exception", "traceback",
        "http 4", "http 5", "timed out",
    ])

