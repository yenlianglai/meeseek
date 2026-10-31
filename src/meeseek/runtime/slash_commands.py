"""Deterministic slash commands: a before_model_callback runs the command
directly or transfers to the owning skill's agent, bypassing the LLM."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable


from meeseek.config import cfg
from meeseek.runtime.skills import Skill

logger = logging.getLogger(__name__)

_SLASH_RE = re.compile(r"^/(\w+)\s*(.*)", re.DOTALL)

# Every slash command, in one table (`COMMANDS`): the UI palette, the router
# prompt and the dispatcher are all derived from it. Each command either `run`s
# a callable (imported lazily so this module stays import-cheap) or transfers to
# a `skill`'s agent, never both. /meetings exists only while
# `workflows.meeting_notes` is on.


def _process_meetings() -> str:
    from meeseek.context.meeting_notes import process_meetings

    return process_meetings()


def _sync_drive() -> str:
    from meeseek.context.sync import sync

    return sync()


def _plan_my_day() -> str:
    from meeseek.runtime.daily_plan import plan_my_day

    return plan_my_day()


@dataclass(frozen=True)
class SlashCommand:
    name: str
    args: str            # what follows the command, as the palette shows it ("<topic>", "")
    description: str
    run: Callable[[], str] | None = None   # runs here, synchronously
    skill: str = ""                        # or transfers to this skill's agent
    featured: bool = False                 # offered as a button in the chat view's empty state

    def __post_init__(self) -> None:
        if bool(self.run) == bool(self.skill):
            raise ValueError(f"/{self.name}: exactly one of run= or skill= must be set")

    @property
    def target(self) -> str:
        return self.run.__name__.lstrip("_") if self.run else Skill._to_agent_name(self.skill)

    @property
    def long_running(self) -> bool:
        """A `run` command executes inside before_model_callback, so the SSE
        stream stays silent for its whole duration; a skill transfer streams its
        tool calls as they happen."""
        return self.run is not None


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("plan", "", "Plan my day from calendar, tickets and open threads",
                 run=_plan_my_day, featured=True),
    SlashCommand("healthcheck", "[focus]", "Sprint health: progress, blockers, risks",
                 skill="sprint", featured=True),
    SlashCommand("code_review", "<MR>", "Review a merge request with inline comments",
                 skill="code", featured=True),
    SlashCommand("debug", "[issue]", "Systematic infra debugging (K8s, logs, metrics)",
                 skill="infra"),
    SlashCommand("research", "<topic>", "Deep research with web + knowledge base",
                 skill="research"),
    SlashCommand("refine", "[topic]", "Refine or correct the knowledge base",
                 skill="research"),
    *([SlashCommand("meetings", "", "Process meeting recordings into notes",
                    run=_process_meetings)] if cfg.workflows.meeting_notes else []),
    SlashCommand("sync", "", "Sync Google Drive docs into the knowledge base",
                 run=_sync_drive),
)

_BY_NAME = {c.name: c for c in COMMANDS}


def slash_command_list() -> list[dict]:
    """Every command the dispatcher answers to, for the UI's palette (served over HTTP).

    Featured commands come first, because the chat view renders them as buttons
    in the order they arrive; the rest follow sorted by name, the palette's order.
    """
    out = [{"name": c.name, "args": c.args, "description": c.description,
            "long_running": c.long_running, "featured": c.featured} for c in COMMANDS]
    return ([c for c in out if c["featured"]]
            + sorted((c for c in out if not c["featured"]), key=lambda c: c["name"]))


def slash_command_tables() -> tuple[str, str]:
    """Markdown tables for the router prompt, generated from COMMANDS."""
    tool_rows = ["| Command | 動作（直接呼叫 orchestrator 工具） |", "|---------|------|"]
    agent_rows = ["| Command | 動作（轉交 sub-agent） |", "|---------|------|"]
    for c in COMMANDS:
        if c.run is not None:
            tool_rows.append(f"| `/{c.name}` | 呼叫 `{c.target}()` |")
        else:
            agent_rows.append(f"| `/{c.name} {c.args}`".rstrip() + f" | → {c.target} |")
    return "\n".join(tool_rows), "\n".join(agent_rows)


