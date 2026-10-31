"""plan-my-day is a deterministic workflow — collection is code, not routing."""

import asyncio
from unittest.mock import patch

import pytest

import meeseek.runtime.daily_plan as daily_plan
from meeseek.config import config


class Calls:
    """Stands in for engine.Packs: answers each tool, records what was asked."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    async def call(self, name, args):
        self.asked.append((name, args))
        answer = self.answers[name]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def reads(self):
        return []

    async def close(self):
        pass


@pytest.fixture
def sources(monkeypatch):
    monkeypatch.setattr(config().workflows, "daily_plan", [
        {"label": "信件（{days}d）", "tool": "search_gmail", "args": {"query": "newer_than:{days}d", "max_results": 10}},
        {"label": "Sprint tickets", "tool": "jira_get_sprint_issues"},
        {"label": "Open MRs", "tool": "list_mrs"},
    ])


class TestCollect:
    def test_a_dead_source_is_marked_not_fatal(self, sources):
        """Mail being down must not kill the morning brief."""
        calls = Calls({"search_gmail": RuntimeError("no credentials"), "jira_get_sprint_issues": "tickets",
                       "list_mrs": "mrs"})
        out = asyncio.run(daily_plan._collect(calls))
        assert "⚠️ 未讀取" in out and "RuntimeError" in out
        assert "tickets" in out and "mrs" in out  # later steps still ran

    def test_each_source_is_one_call_looking_back_a_day_or_the_weekend(self, sources):
        calls = Calls({"search_gmail": "mail", "jira_get_sprint_issues": "t", "list_mrs": "m"})
        out = asyncio.run(daily_plan._collect(calls))
        name, args = calls.asked[0]
        days = args["query"].removeprefix("newer_than:").removesuffix("d")
        assert name == "search_gmail" and days in ("1", "3") and args["max_results"] == 10
        assert f"## 信件（{days}d）\nmail" in out
        assert [n for n, _ in calls.asked] == ["search_gmail", "jira_get_sprint_issues", "list_mrs"]


class TestWorkflow:
    def test_plan_is_synthesized_and_shipped(self, monkeypatch, tmp_path):
        async def collect(packs):
            return "## 信件\n(none)"

        monkeypatch.setattr(daily_plan, "_collect", collect)
        shipped, asked = {}, {}

        def fake_save(content, category, topic):
            shipped["category"] = category
            p = tmp_path / "plan.md"
            p.write_text(content)
            return p

        async def fake_headless(system, message, **kw):
            asked["message"] = message
            return "### 📋 今日計畫"

        with patch("meeseek.engine.headless", fake_headless), \
             patch("meeseek.context.outputs.save_session", side_effect=fake_save):
            out = daily_plan.plan_my_day()

        assert shipped["category"] == "daily_plans"
        assert "今日計畫" in out and "plan.md" in out
        # the synthesis turn gets the collected raw data, not an empty prompt
        assert "## 信件" in asked["message"]
