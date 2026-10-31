"""plan-my-day as a deterministic workflow, not a routed agent.

The collection is the same every day, so it is plain code: each source in
project.yaml `workflows.daily_plan` is one tool call — meeseek's own or a
pack's (mail, chat, sprint tickets, open MRs). The LLM does only what needs
judgment — deep-diving the interesting threads and writing the priority list.
Run by `/plan`.
"""

from __future__ import annotations

import datetime
import logging

logger = logging.getLogger(__name__)


def _args(args: dict, days: int) -> dict:
    return {k: v.replace("{days}", str(days)) if isinstance(v, str) else v for k, v in (args or {}).items()}


async def _collect(packs) -> str:
    """Read every configured source; a failed one is marked, never fatal."""
    from meeseek.config import cfg
    from meeseek.context import prompts

    # Monday's brief covers the weekend.
    days = 3 if datetime.date.today().weekday() == 0 else 1
    sections = []
    for source in cfg.workflows.daily_plan:
        label = str(source.get("label") or source.get("tool", "?")).replace("{days}", str(days))
        try:
            body = await packs.call(source["tool"], _args(source.get("args") or {}, days))
        except Exception as e:  # a dead source is a line in the report, not a crash
            logger.warning("plan_my_day: %s failed", label, exc_info=True)
            body = prompts.text("runtime/daily_plan#unread", error=f"{type(e).__name__}: {e}")
        sections.append(f"## {label}\n{body}")
    return "\n\n".join(sections)


async def _plan() -> str:
    from meeseek.context import prompts
    from meeseek.engine import Packs, headless

    packs = Packs()
    try:
        collected = await _collect(packs)
        return await headless(
            prompts.text("internal/daily_plan"),
            prompts.text("runtime/daily_plan#user_message", collected=collected),
            agent_name="plan_my_day", model="", max_turns=10, memory=True, packs=packs)
    finally:
        await packs.close()


def plan_my_day() -> str:
    """每日開工規劃：彙整信件、Chat、sprint ticket、open MR，以 lead 視角排出
    今日 Top 3 優先序與需要 unblock 的事項。

    Use this when the user says "排今天", "plan my day", "/plan", "開工",
    "今天要做什麼" or similar.
    """
    from meeseek.context.outputs import save_session
    from meeseek.engine import run_sync

    plan = run_sync(_plan)
    path = save_session(plan, category="daily_plans", topic="daily_plan")
    return f"{plan}\n\n---\n完整計畫已存檔 → {path.name}"
