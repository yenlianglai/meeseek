"""The daily autonomous-turn budget, shared by triggers and task-check wakes."""

import json
import time

from meeseek.config import cfg
from meeseek.runtime import turn_budget


class TestBudget:
    def test_counting_adds_up_within_the_day(self):
        assert turn_budget.turns_today() == 0
        turn_budget.count_turn()
        turn_budget.count_turn()
        assert turn_budget.turns_today() == 2

    def test_yesterdays_count_reads_as_zero(self):
        turn_budget.turns_file().parent.mkdir(parents=True, exist_ok=True)
        turn_budget.turns_file().write_text(json.dumps({"day": "2000-01-01", "n": 40}), encoding="utf-8")
        assert turn_budget.turns_today() == 0
        turn_budget.count_turn()   # counting past midnight starts the new day at 1
        assert turn_budget.turns_today() == 1

    def test_cap_reached_follows_the_config(self, monkeypatch):
        monkeypatch.setattr(cfg.triggers, "daily_turns", 2)
        turn_budget.count_turn()
        assert not turn_budget.cap_reached()
        turn_budget.count_turn()
        assert turn_budget.cap_reached()

    def test_a_cap_of_zero_means_no_cap(self, monkeypatch):
        monkeypatch.setattr(cfg.triggers, "daily_turns", 0)
        for _ in range(3):
            turn_budget.count_turn()
        assert not turn_budget.cap_reached()

    def test_the_reset_lands_within_the_next_day(self):
        s = turn_budget.seconds_until_reset()
        assert 0 < s <= 86401   # a leap second away from exactly one day
        assert time.localtime(time.time() + s + 60).tm_hour == 0
