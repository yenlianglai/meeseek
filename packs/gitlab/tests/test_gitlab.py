"""GitLab tools. Invariants:
  1. `_post_error` — a failed POST is never reported as success.
  2. `_resolve_diff_position` — maps a new-side line to a valid GitLab text
     position, or None (caller falls back), because GitLab 400s otherwise.
  3. `_file_window` — every file clip names its line range and the call that
     reads the rest.
  4. CI logs — `_format_jobs` / `_clean_trace` / `_log_window` back the
     job-level and log tools.
  5. Inline comments on an MR go out in one call.
"""

import pytest

from meeseek_pack_gitlab import gitlab
from meeseek_pack_gitlab.gitlab import (
    _MAX_FILE_CHARS,
    _MAX_LOG_CHARS,
    _clean_trace,
    _file_window,
    _format_jobs,
    _log_window,
    _post_error,
    _resolve_diff_position,
)

_DIFF = """@@ -33,6 +33,8 @@ context header
 line33 context
 line34 context
+line35 added
+line36 added
 line37 context
-line38 removed
 line39 context"""


class TestPostError:
    def test_none_on_success(self):
        assert _post_error({"id": 42}) is None

    def test_scope_hint_on_403(self):
        msg = _post_error({"error": "HTTP 403: Forbidden"})
        assert msg and "403" in msg and "`api` scope" in msg

    def test_scope_hint_on_401(self):
        assert "`api` scope" in _post_error({"error": "HTTP 401: Unauthorized"})

    def test_no_scope_hint_on_other_errors(self):
        msg = _post_error({"error": "HTTP 400: Bad Request"})
        assert msg == "HTTP 400: Bad Request"  # no misleading scope hint


class TestResolveDiffPosition:
    def test_added_line_needs_only_new_line(self):
        assert _resolve_diff_position(_DIFF, 35) == {"new_line": 35}

    def test_context_line_needs_both_sides(self):
        # line34 is unchanged context: old_line == new_line == 34
        assert _resolve_diff_position(_DIFF, 34) == {"new_line": 34, "old_line": 34}

    def test_context_line_after_additions_keeps_old_offset(self):
        # line37 is context; two lines were added above it → old_line lags by 2
        assert _resolve_diff_position(_DIFF, 37) == {"new_line": 37, "old_line": 35}

    def test_line_outside_diff_returns_none(self):
        assert _resolve_diff_position(_DIFF, 999) is None

    def test_empty_diff_returns_none(self):
        assert _resolve_diff_position("", 10) is None


class TestFileWindow:
    """Every clip names the line range it returned and the call that reads
    the rest; a bare "(truncated)" gets answered as if it were the whole file.
    """

    def _file(self, lines):
        return "\n".join(lines)

    def test_short_file_comes_back_whole_with_no_continue_hint(self):
        out = _file_window("a\nb\nc", "f.py", "src/f.py", "main", "grp/repo", 5, 1, 0)
        assert "a\nb\nc" in out
        assert "lines 1-3 of 3" in out
        assert "continue with" not in out

    def test_long_file_is_clipped_on_a_line_boundary_with_a_continue_call(self):
        line = "x" * 100
        content = self._file([line] * 1000)  # ~101k chars, way over one result
        out = _file_window(content, "f.py", "src/f.py", "main", "grp/repo", len(content), 1, 0)
        assert len(out) < _MAX_FILE_CHARS + 500
        assert "Not the whole file" in out
        # The hint has to be a call that works, including the non-default repo.
        assert "get_file('src/f.py', ref='main', project='grp/repo', start_line=" in out
        # No partial line: everything kept is a whole line of the original.
        body = out.split("```")[1]
        assert all(kept == line for kept in body.strip().split("\n"))

    def test_the_continue_line_resumes_exactly_where_it_stopped(self):
        content = self._file([f"line{i}" for i in range(1, 4001)])
        first = _file_window(content, "f.py", "src/f.py", "main", "", len(content), 1, 0)
        resume = int(first.split("start_line=")[1].split(")")[0])
        second = _file_window(content, "f.py", "src/f.py", "main", "", len(content), resume, 0)
        assert f"line{resume}" in second.split("```")[1].split("\n")[1]
        assert f"lines {resume}-" in second

    def test_num_lines_windows_without_pretending_it_is_the_whole_file(self):
        content = self._file([f"line{i}" for i in range(1, 101)])
        out = _file_window(content, "f.py", "src/f.py", "main", "", len(content), 10, 5)
        assert "lines 10-14 of 100" in out
        assert "start_line=15" in out
        assert "line15" not in out.split("```")[1]

    def test_start_line_past_the_end_says_so(self):
        out = _file_window("a\nb", "f.py", "src/f.py", "main", "", 3, 99, 0)
        assert "has 2 lines" in out and "past the end" in out

    def test_empty_file_is_not_reported_as_a_clip(self):
        out = _file_window("", "f.py", "src/f.py", "main", "", 0, 1, 0)
        assert "empty" in out and "continue" not in out

    def test_one_absurdly_long_line_says_it_was_clipped_mid_line(self):
        out = _file_window("y" * 60_000, "f.py", "src/f.py", "main", "", 60_000, 1, 0)
        assert "clipped mid-line" in out
        assert len(out) < _MAX_FILE_CHARS + 500

    def test_garbage_line_arguments_fall_back_to_the_whole_file(self):
        # A model can send "1" or None; that must not lose the file.
        out = _file_window("a\nb", "f.py", "src/f.py", "main", "", 3, None, "x")
        assert "lines 1-2 of 2" in out


_JOBS = [
    {"id": 101, "name": "lint", "stage": "test", "status": "success", "duration": 12.4},
    {"id": 102, "name": "pytest", "stage": "test", "status": "failed",
     "duration": 88.0, "failure_reason": "script_failure"},
    {"id": 103, "name": "flaky-e2e", "stage": "test", "status": "failed",
     "allow_failure": True, "failure_reason": "script_failure"},
    {"id": 104, "name": "deploy-uat", "stage": "deploy", "status": "skipped"},
]


class TestFormatJobs:
    """The job list exists so the agent can find WHICH job failed, then its log:
    each job's status and its id (get_job_log's only argument) are load-bearing."""

    def test_every_job_carries_its_id_and_status(self):
        out = _format_jobs(_JOBS, 555, "grp/repo")
        for job in _JOBS:
            assert f"job {job['id']}" in out
            assert job["name"] in out

    def test_failed_jobs_are_named_with_the_call_that_reads_their_log(self):
        out = _format_jobs(_JOBS, 555, "grp/repo")
        assert "get_job_log(102)" in out
        assert "all failed job ids: 102, 103" in out

    def test_failure_reason_is_kept(self):
        # runner_system_failure vs script_failure is a different investigation.
        out = _format_jobs(_JOBS, 555, "grp/repo")
        assert "script_failure" in out

    def test_allow_failure_is_flagged_so_a_red_job_is_not_over_read(self):
        out = _format_jobs(_JOBS, 555, "grp/repo")
        flaky = next(ln for ln in out.split("\n") if "flaky-e2e" in ln)
        assert "allow_failure" in flaky

    def test_jobs_are_grouped_by_stage(self):
        out = _format_jobs(_JOBS, 555, "grp/repo")
        assert "stage `test`" in out and "stage `deploy`" in out
        assert out.index("stage `test`") < out.index("stage `deploy`")

    def test_all_green_pipeline_suggests_nothing_to_read(self):
        green = [{"id": 1, "name": "lint", "stage": "test", "status": "success"}]
        assert "get_job_log" not in _format_jobs(green, 1, "grp/repo")

    def test_pipeline_with_no_jobs_says_why_rather_than_looking_empty(self):
        out = _format_jobs([], 555, "grp/repo")
        assert "no jobs" in out and "555" in out


class TestCleanTrace:
    """A raw trace is terminal output; unstripped it is mostly escape bytes."""

    def test_ansi_colour_codes_are_stripped(self):
        raw = "\x1b[32;1m$ pytest\x1b[0;m\n\x1b[31mFAILED test_a\x1b[0m"
        assert _clean_trace(raw) == "$ pytest\nFAILED test_a"

    def test_section_markers_are_dropped(self):
        raw = ("section_start:1700000000:step_script\r\x1b[0K"
               "\n$ pytest\n"
               "section_end:1700000009:step_script\r\x1b[0K")
        assert _clean_trace(raw) == "$ pytest"

    def test_progress_bar_rewrites_keep_only_the_final_state(self):
        raw = "Downloading  10%\rDownloading  60%\rDownloading 100%"
        assert _clean_trace(raw) == "Downloading 100%"

    def test_plain_log_is_unchanged(self):
        assert _clean_trace("line1\nline2") == "line1\nline2"

    def test_cleaning_a_coloured_trace_actually_shrinks_it(self):
        raw = "".join(f"\x1b[32m line {i}\x1b[0m\n" for i in range(200))
        assert len(_clean_trace(raw)) < len(raw) * 0.8


class TestLogWindow:
    """The failure is at the END of a CI log, and long logs must not silently
    clip: an unannounced clip gets answered as if it were the whole run."""

    def _log(self, n):
        return "\n".join(f"line{i}" for i in range(1, n + 1))

    def test_short_log_comes_back_whole(self):
        out = _log_window(self._log(3), 200, "")
        assert "line1" in out and "line3" in out
        assert "lines 1-3 of 3" in out
        assert "not shown" not in out

    def test_tail_returns_the_end_and_says_what_it_skipped(self):
        out = _log_window(self._log(1000), 50, "")
        assert "line1000" in out
        assert "line951" in out and "line950\n" not in out
        assert "lines 951-1000 of 1000" in out
        assert "950 earlier lines not shown" in out

    def test_a_huge_tail_is_capped_from_the_front_keeping_the_end(self):
        # 5000 lines × ~110 chars ≫ one result: the last lines must survive.
        big = "\n".join(f"line{i} " + "x" * 100 for i in range(1, 5001))
        out = _log_window(big, 5000, "")
        assert len(out) < _MAX_LOG_CHARS + 500
        assert "line5000 " in out
        assert "earlier lines not shown" in out

    def test_search_returns_matching_lines_with_line_numbers(self):
        log = "ok\nTraceback (most recent call last):\nok\nE   AssertionError\n"
        out = _log_window(log, 200, "traceback|assertionerror")
        assert "2: Traceback (most recent call last):" in out
        assert "4: E   AssertionError" in out
        assert "1: ok" not in out
        assert "2 of 2 lines matching" in out

    def test_search_with_no_hits_says_so_instead_of_returning_the_tail(self):
        out = _log_window(self._log(10), 200, "OutOfMemory")
        assert "No line" in out and "matches" in out
        assert "line10" not in out

    def test_search_that_matches_everything_is_clipped_with_a_hint(self):
        big = "\n".join(f"error {i} " + "y" * 100 for i in range(1, 3001))
        out = _log_window(big, 200, "error")
        assert len(out) < _MAX_LOG_CHARS + 500
        assert "narrow the search" in out

    def test_invalid_search_regex_is_reported_not_raised(self):
        out = _log_window(self._log(5), 200, "unclosed(")
        assert "Invalid search regex" in out

    def test_empty_log_says_the_job_may_not_have_started(self):
        assert "empty" in _log_window("", 200, "")

    def test_garbage_tail_lines_falls_back_to_the_default(self):
        out = _log_window(self._log(1000), None, "")
        assert "lines 801-1000 of 1000" in out

    @pytest.mark.parametrize("tail", [0, -5])
    def test_nonpositive_tail_still_returns_something(self, tail):
        out = _log_window(self._log(10), tail, "")
        assert "line10" in out

    def test_a_last_line_bigger_than_the_budget_is_clipped_not_dropped(self):
        """A single line larger than the budget is clipped, not dropped: an
        empty fence and a backwards `[log lines 3-2 of 2]` read as "the job
        logged nothing"."""
        out = _log_window("first line\n" + "X" * 40_000, 200, "")
        body = out.split("```")[1]
        assert body.strip(), "empty fence — the whole log was dropped"
        assert "XXX" in body and "clipped mid-line" in out
        assert "[log lines 2-2 of 2]" in out, "line numbering must not run backwards"


class TestCreateMr:
    """create_mr 的守門邏輯 — 它刻意不推 code，只在 branch 已存在時開 MR。
    HTTP 層被換掉：驗證的是「要不要打那個 POST」的判斷，不是 GitLab 本身。"""

    def _patch(self, monkeypatch, *, get, post):
        """接管 gitlab.py 的 GET/POST，並記下所有被打出去的 POST。"""
        posted: list[tuple[str, dict]] = []

        def _post(endpoint, payload):
            posted.append((endpoint, payload))
            return post(endpoint, payload) if callable(post) else post

        monkeypatch.setattr(gitlab, "_gitlab_get", get)
        monkeypatch.setattr(gitlab, "_gitlab_post", _post)
        return posted

    def test_missing_branch_is_refused_without_creating_anything(self, monkeypatch):
        """branch 不在遠端 → 不可以開 MR，也絕不能打出任何 POST。"""
        posted = self._patch(
            monkeypatch,
            get=lambda ep: {"error": "HTTP 404: Not Found"},
            post={"iid": 1},
        )
        out = gitlab.gitlab_create_mr("no-such-branch", "title")
        assert "does not exist" in out
        assert posted == [], "branch 不存在卻仍打了 POST"

    def test_connection_failure_is_not_reported_as_missing_branch(self, monkeypatch):
        """連不上 ≠ branch 不存在 — 後者會叫人去重推一條already在的 branch。"""
        posted = self._patch(
            monkeypatch,
            get=lambda ep: {"error": "Connection failed: timed out"},
            post={"iid": 1},
        )
        out = gitlab.gitlab_create_mr("feature-x", "title")
        assert "does not exist" not in out
        assert "could not check branch" in out
        assert posted == []

    def test_existing_branch_opens_mr_against_repo_default_branch(self, monkeypatch):
        """target 留空 → 用 repo 自己的 default_branch，不是硬寫 main。"""
        def _get(ep):
            if "repository/branches" in ep:
                return {"name": "feature-x"}
            return {"default_branch": "develop"}

        posted = self._patch(
            monkeypatch, get=_get,
            post={"iid": 42, "title": "title", "web_url": "https://gl/mr/42"},
        )
        out = gitlab.gitlab_create_mr("feature-x", "title", description="body")
        assert "MR !42 created" in out
        assert len(posted) == 1
        endpoint, payload = posted[0]
        assert endpoint.endswith("/merge_requests")
        assert payload["source_branch"] == "feature-x"
        assert payload["target_branch"] == "develop"
        assert payload["description"] == "body"

    def test_explicit_target_branch_wins_over_the_default(self, monkeypatch):
        posted = self._patch(
            monkeypatch,
            get=lambda ep: {"name": "feature-x"} if "branches" in ep else {"default_branch": "develop"},
            post={"iid": 7, "title": "t", "web_url": "u"},
        )
        gitlab.gitlab_create_mr("feature-x", "t", target_branch="release")
        assert posted[0][1]["target_branch"] == "release"

    def test_same_source_and_target_is_refused(self, monkeypatch):
        posted = self._patch(
            monkeypatch,
            get=lambda ep: {"name": "main"} if "branches" in ep else {"default_branch": "main"},
            post={"iid": 1},
        )
        out = gitlab.gitlab_create_mr("main", "t")
        assert "source and target are both" in out
        assert posted == []

    def test_failed_post_is_not_reported_as_success(self, monkeypatch):
        """403（token 少 api scope）不可以印成「MR 開好了」。"""
        self._patch(
            monkeypatch,
            get=lambda ep: {"name": "feature-x"} if "branches" in ep else {"default_branch": "main"},
            post={"error": "HTTP 403: Forbidden"},
        )
        out = gitlab.gitlab_create_mr("feature-x", "t")
        assert "Failed to create MR" in out
        assert "created" not in out
        assert "`api` scope" in out

    def test_empty_description_is_not_sent_as_a_field(self, monkeypatch):
        posted = self._patch(
            monkeypatch,
            get=lambda ep: {"name": "feature-x"} if "branches" in ep else {"default_branch": "main"},
            post={"iid": 3, "title": "t", "web_url": "u"},
        )
        gitlab.gitlab_create_mr("feature-x", "t")
        assert "description" not in posted[0][1]

    def test_repo_outside_the_allowlist_is_refused(self, monkeypatch):
        posted = self._patch(monkeypatch, get=lambda ep: {"name": "b"}, post={"iid": 1})
        with pytest.raises(ValueError, match="允許清單"):
            gitlab.gitlab_create_mr("feature-x", "t", project="some/other-repo")
        assert posted == []


class TestUpdateMr:
    """update_mr — 重點在 append 模式不可以把別人寫的內容弄丟。"""

    def _patch(self, monkeypatch, *, get, put):
        sent: list[tuple[str, dict]] = []

        def _put(endpoint, payload):
            sent.append((endpoint, payload))
            return put(endpoint, payload) if callable(put) else put

        monkeypatch.setattr(gitlab, "_gitlab_get", get)
        monkeypatch.setattr(gitlab, "_gitlab_put", _put)
        return sent

    def test_no_fields_given_is_refused_without_a_request(self, monkeypatch):
        sent = self._patch(monkeypatch, get=lambda ep: {}, put={"web_url": "u"})
        out = gitlab.gitlab_update_mr(7)
        assert "Nothing to update" in out
        assert sent == []

    def test_replace_sends_the_text_verbatim_without_reading_first(self, monkeypatch):
        def _get(ep):
            raise AssertionError("replace 模式不該去讀現有 description")

        sent = self._patch(monkeypatch, get=_get, put={"web_url": "u"})
        out = gitlab.gitlab_update_mr(7, description="brand new body")
        assert sent[0][1]["description"] == "brand new body"
        assert "replaced" in out

    def test_append_keeps_the_existing_description(self, monkeypatch):
        sent = self._patch(
            monkeypatch,
            get=lambda ep: {"description": "human wrote this"},
            put={"web_url": "u"},
        )
        out = gitlab.gitlab_update_mr(7, description="agent note", append_description=True)
        body = sent[0][1]["description"]
        assert body.startswith("human wrote this")
        assert body.endswith("agent note")
        assert "appended" in out

    def test_append_to_an_empty_description_does_not_leave_blank_lines(self, monkeypatch):
        sent = self._patch(
            monkeypatch, get=lambda ep: {"description": None}, put={"web_url": "u"}
        )
        gitlab.gitlab_update_mr(7, description="agent note", append_description=True)
        assert sent[0][1]["description"] == "agent note"

    def test_append_refuses_when_the_current_body_cannot_be_read(self, monkeypatch):
        """讀不到現況就 append，會變成覆蓋 — 必須拒絕，不可以送出 PUT。"""
        sent = self._patch(
            monkeypatch,
            get=lambda ep: {"error": "HTTP 500: Internal Server Error"},
            put={"web_url": "u"},
        )
        out = gitlab.gitlab_update_mr(7, description="agent note", append_description=True)
        assert "could not read its current description" in out
        assert sent == [], "讀失敗卻仍送出 PUT — 會覆蓋掉原本的內容"

    def test_append_without_text_is_refused(self, monkeypatch):
        sent = self._patch(monkeypatch, get=lambda ep: {}, put={"web_url": "u"})
        out = gitlab.gitlab_update_mr(7, append_description=True)
        assert "Nothing to update" in out
        assert sent == []

    def test_untouched_fields_are_not_sent(self, monkeypatch):
        """只改 title 時，payload 不可以夾帶 description/target_branch。"""
        sent = self._patch(monkeypatch, get=lambda ep: {}, put={"web_url": "u"})
        gitlab.gitlab_update_mr(7, title="new title")
        assert sent[0][1] == {"title": "new title"}

    def test_failed_put_is_not_reported_as_success(self, monkeypatch):
        self._patch(monkeypatch, get=lambda ep: {}, put={"error": "HTTP 403: Forbidden"})
        out = gitlab.gitlab_update_mr(7, title="t")
        assert "Failed to update" in out
        assert "updated:" not in out
        assert "`api` scope" in out

    def test_repo_outside_the_allowlist_is_refused(self, monkeypatch):
        sent = self._patch(monkeypatch, get=lambda ep: {}, put={"web_url": "u"})
        with pytest.raises(ValueError, match="允許清單"):
            gitlab.gitlab_update_mr(7, title="t", project="some/other-repo")
        assert sent == []


class TestInlineCommentsInOneCall:
    """A review's comments go out in one call: the diff is read once, the posts
    run in parallel, and a failed one is named without losing the others."""

    @pytest.fixture
    def api(self, monkeypatch):
        calls = {"changes": 0, "posts": [], "head": "h1"}

        def get(endpoint):
            if endpoint.endswith("/changes"):
                calls["changes"] += 1
                return {"diff_refs": {"base_sha": "b", "head_sha": calls["head"], "start_sha": "s"},
                        "changes": [{"new_path": "app.py", "old_path": "app.py", "diff": _DIFF}]}
            return {"sha": calls["head"]}

        def post(endpoint, payload):
            calls["posts"].append((endpoint, payload))
            if "line37" in payload["body"]:
                return {"error": "HTTP 403: Forbidden"}
            return {"id": "d1", "notes": [{"id": 7}]}

        monkeypatch.setattr(gitlab, "_gitlab_get", get)
        monkeypatch.setattr(gitlab, "_gitlab_post", post)
        monkeypatch.setattr(gitlab, "_resolve_project", lambda p: "g/app")
        gitlab._changes_cache.clear()
        return calls

    def test_each_outcome_is_listed_and_a_failure_is_counted(self, api):
        out = gitlab.gitlab_comment_mr_lines(5, [
            {"file_path": "app.py", "new_line": 35, "body": "line35"},
            {"file_path": "app.py", "new_line": 37, "body": "line37"},
            {"file_path": "app.py", "new_line": 999, "body": "far away"},
        ])
        first = out.splitlines()[0]
        assert first == "2 of 3 comments posted on !5; 1 FAILED:"
        assert "`app.py:37`: FAILED — HTTP 403" in out
        assert "`app.py:999`: not in the diff" in out
        assert api["changes"] == 1
        inline = [p for e, p in api["posts"] if e.endswith("/discussions")]
        assert {p["position"]["new_line"] for p in inline} == {35, 37}

    def test_the_diff_is_cached_until_the_mr_is_pushed(self, api):
        one = [{"file_path": "app.py", "new_line": 35, "body": "x"}]
        gitlab.gitlab_comment_mr_lines(5, one)
        gitlab.gitlab_comment_mr_lines(5, one)
        assert api["changes"] == 1
        api["head"] = "h2"
        gitlab.gitlab_comment_mr_lines(5, one)
        assert api["changes"] == 2

    def test_a_malformed_comment_posts_nothing(self, api):
        out = gitlab.gitlab_comment_mr_lines(5, [{"file_path": "app.py", "body": "no line"}])
        assert out.startswith("Nothing was posted") and api["posts"] == []


class TestReplyMrComment:
    def _calls(self, monkeypatch, post=None, put=None):
        calls = []
        monkeypatch.setattr(gitlab, "_gitlab_post",
                            lambda ep, body: calls.append(("POST", ep, body)) or (post or {"id": 7}))
        monkeypatch.setattr(gitlab, "_gitlab_put",
                            lambda ep, body: calls.append(("PUT", ep, body)) or (put or {}))
        return calls

    def test_the_reply_goes_into_the_thread(self, monkeypatch):
        calls = self._calls(monkeypatch)
        out = gitlab.gitlab_reply_mr_comment(73, " abc123 ", "已經修了", project="acme/acme-app/iac")
        assert calls == [("POST", "projects/acme%2Facme-app%2Fiac/merge_requests/73/discussions/abc123/notes", {"body": "已經修了"})]
        assert "note #7" in out and "#note_7" in out and "resolved" not in out

    def test_resolve_marks_the_thread_resolved_after_replying(self, monkeypatch):
        calls = self._calls(monkeypatch)
        out = gitlab.gitlab_reply_mr_comment(73, "abc123", "done", resolve=True, project="acme/acme-app/iac")
        assert calls[1] == ("PUT", "projects/acme%2Facme-app%2Fiac/merge_requests/73/discussions/abc123", {"resolved": True})
        assert out.endswith("Thread resolved.")

    def test_a_resolve_that_fails_is_not_reported_as_done(self, monkeypatch):
        self._calls(monkeypatch, put={"error": "HTTP 400: cannot resolve"})
        out = gitlab.gitlab_reply_mr_comment(73, "abc123", "done", resolve=True, project="acme/acme-app/iac")
        assert "Reply posted" in out and "NOT resolved" in out

    def test_an_unknown_thread_points_at_get_mr_comments(self, monkeypatch):
        calls = self._calls(monkeypatch, post={"error": "HTTP 404: Not Found"})
        out = gitlab.gitlab_reply_mr_comment(73, "nope", "hi", resolve=True, project="acme/acme-app/iac")
        assert out.startswith("Failed") and "get_mr_comments" in out
        assert [c[0] for c in calls] == ["POST"]   # nothing resolved after a failed reply


def test_comment_listing_names_each_thread(monkeypatch):
    monkeypatch.setattr(gitlab, "_gitlab_get", lambda ep: [
        {"id": "d1", "notes": [{"author": {"name": "Wayne"}, "body": "這裡?", "created_at": "2026-10-20"}]}])
    assert "thread d1" in gitlab.gitlab_get_mr_comments(73, project="acme/acme-app/iac")


def test_an_html_error_page_is_not_reported_as_a_permission_problem(monkeypatch):
    """2026-10-16: a 403 page from in front of GitLab was told to the user as 'no permission'."""
    monkeypatch.setattr(gitlab.http, "request", lambda *a, **k: {
        "error": 'HTTP 403: <!doctype html><meta charset="utf-8"><title>403</title>403 Forbidden', "url": "u"})
    err = gitlab._gitlab_get("projects/x/merge_requests")["error"]
    assert err.startswith("HTTP 403") and "VPN" in err and "doctype" not in err


_MR = {"iid": 73, "title": "t", "state": "opened", "author": {"name": "A"}, "source_branch": "s",
       "target_branch": "main", "created_at": "2026-10-01", "updated_at": "2026-10-02",
       "web_url": "https://gitlab.example/acme/acme-app/iac/-/merge_requests/73"}


class TestWholeText:
    """2026-10-29: descriptions cut at 500 characters sent Claude back to GitLab ~130 times."""

    def test_a_long_description_comes_back_whole(self, monkeypatch):
        desc = "intro\n" + "x" * 8000 + "\nLAST LINE"
        monkeypatch.setattr(gitlab, "_gitlab_get", lambda ep: {**_MR, "description": desc})
        out = gitlab.gitlab_get_mr(73, project="acme/acme-app/iac")
        assert desc in out and "truncated" not in out

    def test_an_over_limit_description_says_how_much_is_missing_and_where(self, monkeypatch):
        desc = "x" * (gitlab._MAX_TEXT_CHARS + 1234)
        monkeypatch.setattr(gitlab, "_gitlab_get", lambda ep: {**_MR, "description": desc})
        out = gitlab.gitlab_get_mr(73, project="acme/acme-app/iac")
        assert "[truncated: 1234 more characters" in out and _MR["web_url"] in out

    def test_long_comments_and_replies_come_back_whole(self, monkeypatch):
        first, reply = "q" * 3000 + " END1", "r" * 3000 + " END2"
        monkeypatch.setattr(gitlab, "_gitlab_get", lambda ep: [{"id": "d1", "notes": [
            {"author": {"name": "W"}, "body": first, "created_at": "2026-10-20"},
            {"author": {"name": "R"}, "body": reply, "created_at": "2026-10-21"}]}])
        out = gitlab.gitlab_get_mr_comments(73, project="acme/acme-app/iac")
        assert first in out and reply in out
