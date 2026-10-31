"""Tests for DCI (Direct Corpus Interaction) knowledge base primitives."""

from meeseek.tools.dci import kb_grep, kb_list, kb_read


class TestKbGrep:
    def test_finds_english_literal(self, tmp_kb):
        result = kb_grep("API endpoint")
        assert "doc1.md" in result
        assert "/api/v1/npc" in result

    def test_finds_chinese_substring(self, tmp_kb):
        result = kb_grep("Redis")
        assert "doc2.md" in result
        assert "session" in result

    def test_regex_pattern(self, tmp_kb):
        result = kb_grep(r"character.?id")
        assert "doc1.md" in result

    def test_invalid_regex_falls_back_to_literal(self, tmp_kb):
        result = kb_grep("character_id (")
        assert isinstance(result, str)
        assert "Error" not in result

    def test_also_matching_chains_constraints(self, tmp_kb):
        # doc1 contains both "API" and "character_id"; doc2 has neither pair
        result = kb_grep("API", also_matching="character_id")
        assert "doc1.md" in result
        assert "doc2.md" not in result

    def test_path_filter(self, tmp_kb):
        result = kb_grep("character", path_filter="notes")
        assert "correction" in result
        assert "doc1.md" not in result

    def test_no_match_suggests_next_step(self, tmp_kb):
        result = kb_grep("xyznonexistent12345")
        assert "No matches" in result

    def test_empty_pattern(self, tmp_kb):
        assert "Error" in kb_grep("")

    def test_empty_kb(self, _home):
        result = kb_grep("anything")
        assert "empty" in result.lower()

    def test_string_ints_tolerated(self, tmp_kb):
        """ADK/engine wrappers may pass ints as strings."""
        result = kb_grep("API", context_lines="1", max_files="3")
        assert "doc1.md" in result


class TestKbList:
    def test_lists_all_files(self, tmp_kb):
        result = kb_list()
        assert "doc1.md" in result
        assert "doc2.md" in result
        assert "2026-01-01_correction.md" in result

    def test_subdir_filter(self, tmp_kb):
        result = kb_list("notes")
        assert "correction" in result
        assert "doc1.md" not in result

    def test_excludes_outputs_dir(self, tmp_kb):
        outputs = tmp_kb / "outputs"
        (outputs / "report.md").write_text("# generated report")
        assert "report.md" not in kb_list()
        assert "report.md" not in kb_grep("generated")

    def test_excludes_what_is_already_injected_and_the_seed(self, tmp_kb):
        notes = tmp_kb / "notes"
        for sub in ("pinned", "examples"):
            (notes / sub).mkdir()
            (notes / sub / "team.md").write_text("MARKER")
        (notes / "facts.md").write_text("- MARKER (2026-10-01, explicit)")
        assert kb_grep("MARKER").startswith("No matches")
        assert "pinned" not in kb_list("notes")
        assert "correction" in kb_grep("npc_id")


class TestKbRead:
    def test_read_by_relative_path(self, tmp_kb):
        result = kb_read("sources/drive/doc1.md")
        assert "Design Doc" in result
        assert "lines 1-" in result

    def test_read_by_bare_filename(self, tmp_kb):
        result = kb_read("doc2.md")
        assert "Redis" in result

    def test_read_window(self, tmp_kb):
        (tmp_kb / "sources" / "drive" / "long.md").write_text(
            "\n".join(f"line {i}" for i in range(1, 101))
        )
        result = kb_read("long.md", start_line=10, num_lines=5)
        assert "line 10" in result
        assert "line 15" not in result
        assert "start_line=15" in result  # continuation hint

    def test_missing_file(self, tmp_kb):
        assert "not found" in kb_read("nope.md")

    def test_path_escape_blocked(self, tmp_kb):
        result = kb_read("../../etc/passwd")
        assert "escapes" in result or "not found" in result


class TestKbReadBudget:
    def test_truncation_keeps_footer_and_resume_hint(self, tmp_kb):
        # 400 lines × ~120 chars ≈ 48k chars: more than one window can hold.
        (tmp_kb / "sources" / "drive" / "wide.md").write_text(
            "\n".join(f"line {i} " + "x" * 110 for i in range(1, 401))
        )
        result = kb_read("wide.md", num_lines=400)
        from meeseek.tools.dci import _MAX_OUTPUT_CHARS

        assert len(result) <= _MAX_OUTPUT_CHARS
        assert "of 400]" in result
        assert "stopped early" in result
        # The hint points at the line right after the last one actually shown.
        import re

        shown = [int(m) for m in re.findall(r"^\s*(\d+)  line", result, flags=re.M)]
        assert result.rstrip().endswith(f"start_line={shown[-1] + 1}).")
        # Whole lines only — no half line at the cut.
        assert result.count("line ") == len(shown)

    def test_single_oversized_line_is_clipped_not_dropped(self, tmp_kb):
        (tmp_kb / "sources" / "drive" / "huge.md").write_text("a" * 60_000 + "\nsecond")
        result = kb_read("huge.md", num_lines=2)
        assert "(line clipped)" in result
        assert "start_line=2" in result

    def test_start_past_end_is_an_error(self, tmp_kb):
        assert "past the end" in kb_read("doc1.md", start_line=9999)


class TestSaveRefinement:
    def test_a_dated_topic_is_not_dated_twice(self, tmp_path, monkeypatch):
        import importlib.util
        from datetime import date

        from meeseek.config import home

        spec = importlib.util.spec_from_file_location("research_tools", home().skills / "research" / "tools.py")
        tools = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tools)
        monkeypatch.setattr(tools, "refinement_dir", lambda: tmp_path)

        tools.save_refinement("2026-06-29_npc actions", "x")
        assert [p.name for p in tmp_path.iterdir()] == [f"{date.today().isoformat()}_npc_actions.md"]
