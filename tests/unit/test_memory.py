"""Long-term memory: every fact line carries date + source, new facts are
written only as `explicit`, and facts age out of the 90-day window."""

from datetime import date, timedelta

import pytest

from meeseek.context import memory


@pytest.fixture
def facts(tmp_path, monkeypatch):
    monkeypatch.setattr("meeseek.runtime.learned_rules.load_learned_rules", lambda skill: "")
    return tmp_path


class TestAppendFact:
    def test_the_line_carries_date_and_source(self, facts):
        out = memory.append_fact("prod 叫 acme-prod")
        line = memory.facts_file().read_text(encoding="utf-8").strip().splitlines()[-1]
        assert line == f"- prod 叫 acme-prod ({date.today().isoformat()}, explicit)"
        assert "explicit" in out

    def test_a_source_other_than_explicit_is_refused_and_nothing_is_written(self, facts):
        for bad in ("turn-fact", "turn-extract", "user-correction", "legacy", "bogus"):
            with pytest.raises(ValueError, match="不是記憶的來源"):
                memory.append_fact("x", source=bad)
        assert not memory.facts_file().exists()


class TestLoadFacts:
    def test_ages_out_both_the_old_and_the_sourced_line_format(self, facts):
        memory.facts_file().write_text(
            "# Learned Facts\n"
            "- old plain (2020-01-01)\n"
            "- old sourced (2020-01-01, explicit)\n"
            "- new plain (2099-01-01)\n"
            "- new sourced (2099-01-01, turn-fact)\n", encoding="utf-8")
        kept = memory.load_facts(max_age_days=90)
        assert "old plain" not in kept and "old sourced" not in kept, "a sourced line must still expire"
        assert "new plain" in kept and "new sourced" in kept

    def test_lines_under_older_source_tags_still_load(self, facts):
        recent = (date.today() - timedelta(days=5)).isoformat()
        memory.facts_file().write_text(
            "# Learned Facts\n"
            f"- a ({recent}, turn-fact)\n"
            f"- b ({recent}, turn-extract)\n"
            f"- c ({recent}, legacy)\n", encoding="utf-8")
        kept = memory.load_facts()
        assert all(f"- {x} (" in kept for x in "abc")

    def test_the_window_is_ninety_days(self, facts):
        today = date.today()
        memory.facts_file().write_text(
            "# Learned Facts\n"
            f"- inside ({(today - timedelta(days=89)).isoformat()}, explicit)\n"
            f"- outside ({(today - timedelta(days=90)).isoformat()}, explicit)\n", encoding="utf-8")
        kept = memory.load_facts()
        assert "inside" in kept and "outside" not in kept


class TestMemoryInstruction:
    @pytest.fixture
    def mem(self, facts, monkeypatch):
        pinned = facts / "pinned"
        pinned.mkdir()
        (pinned / "team.md").write_text("# Team\n- Grace", encoding="utf-8")
        (pinned / "README.md").write_text("not injected", encoding="utf-8")
        monkeypatch.setattr(memory, "pinned_dir", lambda: pinned)
        memory.append_fact("prod 叫 acme-prod")
        monkeypatch.setattr("meeseek.runtime.learned_rules.load_learned_rules",
                            lambda skill: "- 標題一律純中文" if skill == "sprint" else "")
        return facts

    def test_a_skill_agent_gets_pinned_facts_and_its_own_rules(self, mem):
        out = memory.memory_instruction("sprint")
        assert "- Grace" in out and "acme-prod" in out and "標題一律純中文" in out
        assert "not injected" not in out

    def test_the_router_gets_no_skill_rules(self, mem):
        out = memory.memory_instruction(None)
        assert "- Grace" in out and "標題一律純中文" not in out

    def test_empty_memory_is_empty(self, facts, monkeypatch):
        monkeypatch.setattr(memory, "pinned_dir", lambda: facts / "none")
        assert memory.memory_instruction("code") == ""


class TestFactDuplicates:
    def test_a_known_fact_is_refused(self, facts, monkeypatch):
        monkeypatch.setattr(memory, "pinned_dir", lambda: facts / "none")
        memory.append_fact("prod 環境的 GCP 專案叫 acme-prod")
        with pytest.raises(ValueError, match="已經記得"):
            memory.append_fact("prod 環境 GCP 專案名稱是 acme-prod")


class TestTidyFacts:
    def test_a_fact_past_its_window_leaves_the_file_and_the_rest_stays(self, facts):
        old = (date.today() - timedelta(days=memory.FACTS_WINDOW_DAYS + 1)).isoformat()
        memory.facts_file().write_text(f"# Learned Facts\n\n## Technical\n- gone ({old}, explicit)\n"
                                     f"- kept ({date.today().isoformat()}, explicit)\n", encoding="utf-8")
        assert memory.tidy_facts() == 1
        text = memory.facts_file().read_text(encoding="utf-8")
        assert "gone" not in text and "- kept" in text and "## Technical" in text


def test_meetings_patterns_reach_every_agent_beside_the_roster(facts, monkeypatch):
    monkeypatch.setattr("meeseek.context.ontology.patterns", lambda: "- Heidi 分派給 Grace（2 場會）")
    block = memory.memory_instruction(None)
    assert "團隊互動" in block and "Heidi 分派給 Grace" in block
