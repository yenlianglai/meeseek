"""The day's autonomous-turn budget — one count for every turn nobody asked for.

Trigger turns (runtime/triggers.py) and a task's check turns (runtime/wakes.py,
kind "task_check") draw on the same count: `triggers.daily_turns` caps them
together. The count lives in turns_file() and reads as 0 once the local date
turns — so what a full day holds back comes due after midnight.
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timedelta

from meeseek.config import cfg
from meeseek.config.paths import home


def turns_file() -> Path:
    return home().data / "trigger_turns.json"


def _today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def turns_today() -> int:
    """Autonomous turns started today (local date); yesterday's count reads as 0."""
    try:
        data = json.loads(turns_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return int(data.get("n", 0)) if data.get("day") == _today() else 0


def count_turn() -> None:
    turns_file().parent.mkdir(parents=True, exist_ok=True)
    turns_file().write_text(json.dumps({"day": _today(), "n": turns_today() + 1}), encoding="utf-8")


def cap_reached() -> bool:
    """The day's budget is spent (a cap of 0 means no cap)."""
    cap = cfg.triggers.daily_turns
    return bool(cap) and turns_today() >= cap


def seconds_until_reset() -> float:
    """Until the next local midnight, when the count reads as 0 again."""
    now = datetime.now().astimezone()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()
