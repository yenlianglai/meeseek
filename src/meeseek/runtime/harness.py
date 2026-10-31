"""The harness: the reply must agree with what the turn's events say it did.

The ledger is a view over the session's function calls and responses, not a
second record. One rule acts on it: a failed write must not go unmentioned —
this turn's, or the last two turns' when the reply talks about their target.
A reply that leaves one out is turned into a `reconcile` call naming the
failure, and the model writes it again; once per turn, after which the plugin
appends the facts itself.
"""

from __future__ import annotations

import logging

from meeseek.core.ledger import (  # noqa: F401 — the harness's rules, shared with the core's policies
    FAILURE_TERMS,
    PROMISE_TERMS,
    Entry,
    failure_of,
    unreported,
    unsettled_failures,
)

logger = logging.getLogger(__name__)

# Writes that are not in the registry: MCP write tools and the tools the
# orchestrator and skills own.
_OWNED_WRITES = frozenset({
    "propose_sop_rule", "forget_sop_rule", "remember_fact",
    "save_refinement", "update_kb_file",
})

# Tools that change something outside the conversation without being a write the
# reply must account for: Drive and meeting syncs into the KB, shipped reports,
# generated images.
_SIDE_EFFECTS = frozenset({
    "sync", "sync_drive", "process_meetings", "ship_output", "generate_image",
})


def _declared(name: str) -> str:
    """read | write | side, as a running MCP server's tool declared it ("" when none did)."""
    from meeseek.app import current

    a = current()
    tools = a.mcp_tools if a is not None else None
    return tools.effects().get(name, "") if tools is not None else ""


def is_write(name: str) -> bool:
    """A write the reply must account for: a registry tool declared `writes`,
    a tool the orchestrator or a skill owns that writes, an MCP tool that
    declares itself a write, or one that declares nothing and whose name is
    not a read."""
    from meeseek.config import cfg
    from meeseek.tools.registry import GATED_TOOLS, READ_PREFIXES

    if name in GATED_TOOLS or name in _OWNED_WRITES:
        return True
    declared = _declared(name)
    if declared:
        return declared == "write"
    mcp_names = {n for server in cfg.mcp_servers for n in server.tool_filter}
    return name in mcp_names and not name.startswith(READ_PREFIXES)


def has_effect(name: str) -> bool:
    """Does calling this tool change anything outside the conversation? Every
    write does, and so does every side-effect tool. A replay answers each such
    call with a stub (runtime/replays.py)."""
    return is_write(name) or name in _SIDE_EFFECTS or _declared(name) == "side"


