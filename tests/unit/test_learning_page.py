"""The Learning page's data and actions: rules with their case, facts with
whether they still reach agents, pin and forget."""

from datetime import date, timedelta

import pytest

from meeseek.context import memory
from meeseek.runtime import learned_rules as lr
from meeseek.runtime import learning_page as page
from dataclasses import replace

from meeseek.config.paths import home, set_home


@pytest.fixture
def store(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    (skills / "demo").mkdir(parents=True)
    (skills / "demo" / "SKILL.md").write_text("---\ndescription: d\n---\nbody")
    set_home(replace(home(), skills=skills))
    monkeypatch.setattr(lr, "_learned_path", lambda s: tmp_path / f"{s}.learned.md")
    return tmp_path


RULE = "開 Jira 票時標題一律用純中文摘要，類型由 issue type 欄位表達"


class TestRules:
    def test_a_rule_is_listed_with_its_id(self, store):
        lr.write_learned_rule("demo", RULE, "使用者糾正", "in-session")
        [r] = page.rules()
        assert r["skill"] == "demo" and r["text"] == RULE and r["id"] == lr.rule_id(RULE)
        assert r["case"] is None

    def test_forget_removes_only_that_rule(self, store):
        lr.write_learned_rule("demo", RULE, "r", "in-session")
        lr.write_learned_rule("demo", "查 log 之前先確認是哪個環境的叢集與 namespace", "r", "in-session")
        page.forget_rule("demo", lr.rule_id(RULE))
        assert [r["text"] for r in page.rules()] == ["查 log 之前先確認是哪個環境的叢集與 namespace"]

    def test_an_unknown_rule_is_an_error(self, store):
        with pytest.raises(ValueError):
            page.forget_rule("demo", "nope")


class TestFacts:
    def test_facts_carry_their_expiry(self, store):
        memory.facts_file().write_text("# Learned Facts\n- 舊事實 (2026-06-01, explicit)\n- 新事實 (2026-10-01, explicit)\n")
        out = page.facts(today=date(2026, 10, 8))
        assert [(f["text"], f["live"]) for f in out] == [("舊事實", False), ("新事實", True)]
        assert out[1]["expires"] == "2026-12-30"

    def test_pin_moves_the_fact_into_a_pinned_note(self, store):
        memory.facts_file().write_text("# Learned Facts\n- prod 叫 acme-prod (2026-10-01, explicit)\n")
        page.pin_fact("- prod 叫 acme-prod (2026-10-01, explicit)")
        assert "prod 叫 acme-prod" in (memory.pinned_dir() / page.PINNED_FACTS).read_text()
        assert page.facts() == []

    def test_forget_deletes_the_fact(self, store):
        memory.facts_file().write_text("# Learned Facts\n- x 事實內容 (2026-10-01, explicit)\n")
        page.forget_fact("- x 事實內容 (2026-10-01, explicit)")
        assert page.facts() == []
        with pytest.raises(ValueError):
            page.forget_fact("- 不存在 (2026-10-01, explicit)")


class TestFactsReachingAgents:
    def test_only_the_newest_facts_reach_agents(self, store, monkeypatch):
        monkeypatch.setattr(memory, "MAX_FACTS", 2)
        today = date.today()
        lines = [f"- 第 {i} 件事實內容 ({(today - timedelta(days=i)).isoformat()}, explicit)" for i in range(3)]
        memory.facts_file().write_text("# Learned Facts\n" + "\n".join(lines) + "\n")
        injected = memory.load_facts()
        assert "第 0 件" in injected and "第 1 件" in injected and "第 2 件" not in injected
        assert [f["live"] for f in page.facts()] == [True, True, False]


class TestConsoleEdits:
    def test_a_rule_written_on_the_console(self, store):
        page.add_rule("demo", RULE, "")
        [r] = page.rules()
        assert r["text"] == RULE and r["source"] == "console"

    def test_editing_a_rule_moves_its_case_and_rechecks_it(self, store):
        lr.write_learned_rule("demo", RULE, "r", "in-session")
        rid = lr.rule_id(RULE)
        (lr.cases_dir() / "demo").mkdir(parents=True)
        (lr.cases_dir() / "demo" / f"{rid}.json").write_text("{}")
        new = "開 Jira 票時標題一律用純中文摘要，不加任何前綴標籤"
        new_id = page.edit_rule("demo", rid, new, "使用者改寫")
        [r] = page.rules()
        assert r["text"] == new and r["id"] == new_id and r["source"] == "in-session"
        assert (lr.cases_dir() / "demo" / f"{new_id}.json").exists()
        with pytest.raises(ValueError):
            page.edit_rule("demo", new_id, "太短", "")

    def test_an_edit_may_not_duplicate_another_rule(self, store):
        lr.write_learned_rule("demo", RULE, "r", "in-session")
        other = "查 log 之前先確認是哪個環境的叢集與 namespace"
        lr.write_learned_rule("demo", other, "r", "in-session")
        with pytest.raises(ValueError):
            page.edit_rule("demo", lr.rule_id(other), RULE + "。", "")
