"""The orchestrator: the agent that answers first and hands work to a skill.

What it is, whichever engine runs it: its name, its instruction (the router
prompt with the slash tables, then the common block) and its own tools — a
Drive sync, the morning plan, rules and facts, Claude Code, its past
conversations, tasks, and the meeting notes when they are on. It holds no
read tools: the skill it hands over to has them all.
"""

from __future__ import annotations

from meeseek.config import cfg
from meeseek.context import prompts
from meeseek.context.memory import remember_fact
from meeseek.context.sync import sync
from meeseek.runtime.learned_rules import make_propose_tool, remove_learned_rule
from meeseek.runtime.skills import common
from meeseek.tools.registry import CLAUDE_TOOLS, REMIND_TOOLS, SESSION_TOOLS, TASK_TOOLS


def name() -> str:
    return cfg.project.name.lower().replace(" ", "_") + "_orchestrator"


def sync_drive() -> str:
    """Sync Google Drive documents to local knowledge base.

    Use this when the user says "sync" or wants to update the local docs.
    """
    return sync()


def forget_sop_rule(skill: str, keyword: str) -> str:
    """移除某 skill 中含指定關鍵字的學習規則（SOP 動態注入區）。

    ⚠️ 只能在使用者於對話中明確同意後呼叫。用於修剪過時、錯誤或不再適用的規則。

    Args:
        skill: 目標 skill 名（如 infra、code）
        keyword: 用來比對要移除的規則的關鍵字（比對規則全文）
    """
    return remove_learned_rule(skill, keyword)


def instruction() -> str:
    """The router's instruction as the policy has it (runtime/policy.py): a
    replay's bundle, or the prompt as it reads after a reload."""
    from meeseek.runtime.slash_commands import slash_command_tables

    slash_tools, slash_agents = slash_command_tables()
    return prompts.text("agents/router", slash_tool_commands=slash_tools, slash_agent_commands=slash_agents,
                        routing_hints=routing_hints()) + common()


def routing_hints() -> str:
    """Each skill's `routes:` (SKILL.md), as the router's lines: what goes where
    beyond the descriptions. The router's prompt names no skill of its own."""
    from meeseek.runtime.skills import discover_skills

    lines = [f"- {r} → {s.agent_name}" for s in discover_skills() for r in s.routes]
    return "\n".join(lines) or "（沒有額外的路由提示：照各 agent 的 description 轉交。）"


def own_tools() -> list:
    """Its plain-function tools; the registry tools it holds are `registry_tools()`."""
    from meeseek.context.meeting_notes import check_meetings_status, process_meetings
    from meeseek.runtime.daily_plan import plan_my_day

    tools: list = [sync_drive, plan_my_day, make_propose_tool(None), forget_sop_rule, remember_fact]
    if cfg.workflows.meeting_notes:
        tools += [process_meetings, check_meetings_status]
    return tools


def registry_tools() -> list[str]:
    # Claude Code: meeseek decides when the stronger model should do the work.
    # Its own past conversations: "what did you tell me last week" is not worth a hand-off.
    # Tasks that outlive the turn (runtime/tasks.py): opened on the user's word in
    # the console, pushed forward by their wakes.
    # Reminders (runtime/wakes.py): "五點提醒我", from the console or the phone.
    return [*CLAUDE_TOOLS, *SESSION_TOOLS, *TASK_TOOLS, *REMIND_TOOLS]
