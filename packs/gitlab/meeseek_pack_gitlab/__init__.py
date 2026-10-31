"""meeseek's GitLab pack: MRs, pipelines, issues and the code of the repos it
is allowed (`config.projects`), as an MCP server (`python -m meeseek_pack_gitlab`).
Reading code goes through a local git mirror (code_read, git_mirror)."""

from meeseek_pack_kit import Tool, params

from meeseek_pack_gitlab.code_read import get_file, search_code
from meeseek_pack_gitlab.gitlab import (
    gitlab_comment_mr,
    gitlab_reply_mr_comment,
    gitlab_comment_mr_lines,
    gitlab_create_mr,
    gitlab_update_mr,
    gitlab_get_job_log,
    gitlab_get_mr,
    gitlab_get_mr_comments,
    gitlab_get_mr_diff,
    gitlab_list_open_mrs,
    gitlab_list_pipeline_jobs,
    gitlab_list_pipelines,
    gitlab_search_issues,
    gitlab_search_mrs,
)
from meeseek_pack_gitlab.review import review_context
from meeseek_pack_gitlab.sources import mr_poll
from meeseek_pack_gitlab.settings import settings


def tools() -> list[Tool]:
    s = settings()
    # Every GitLab tool takes this: without it the call silently answers about the
    # default repo, which looks correct whenever the iid exists in both repos.
    _PROJECT_PARAM = {
        "type": "string",
        "description": f"GitLab repo path (default: {s.project_path}; available: {', '.join(s.projects)})",
    }
    return [
        # --- GitLab (common) ---
        Tool(
            name="list_mrs",
            description=f"List all open MRs in a GitLab repo (default: {s.project_path})",
            fn=gitlab_list_open_mrs,
            parameters=params({"project": _PROJECT_PARAM}),
        ),
        Tool(
            name="review_context",
            description=(
                "Everything a review of one MR starts from, read in parallel in one call: the MR "
                "(title, author, branches, description), its CI status (and failed jobs), the ticket "
                "key named in its title or branch, the existing discussions, and the diff. Use it as "
                "the first step of a review instead of get_mr, get_mr_diff and get_mr_comments one by "
                "one; then read the ticket's acceptance criteria with the issue tracker's tool."),
            fn=lambda mr, project="": review_context(str(mr), project),
            parameters=params({
                "mr": {"type": "string", "description": "MR number ('165', '!165') or MR URL — a URL also names the repo"},
                "project": _PROJECT_PARAM,
            }, ["mr"]),
        ),
        Tool(
            name="get_mr",
            description="Get detailed info for a specific MR (title, author, branch, description)",
            fn=lambda mr_iid, project="": gitlab_get_mr(int(mr_iid), project=project),
            parameters=params({
                "mr_iid": {"type": "integer", "description": "MR number"},
                "project": _PROJECT_PARAM,
            }, ["mr_iid"]),
        ),
        Tool(
            name="get_mr_comments",
            description="Get all comments (notes and discussions) on a MR, including inline code comments with file/line context",
            fn=lambda mr_iid, project="": gitlab_get_mr_comments(int(mr_iid), project=project),
            parameters=params({
                "mr_iid": {"type": "integer", "description": "MR number"},
                "project": _PROJECT_PARAM,
            }, ["mr_iid"]),
        ),
        Tool(
            name="search_code",
            description=(
                "Search a repo's code at any ref (default branch, a branch, an MR or a commit) — "
                "`git grep` over that exact commit: every match with its line number and context lines. "
                "Use it to find a definition or every caller instead of paging through files."),
            fn=lambda query, project="", ref="", path="", context=2: search_code(
                query, project=project, ref=ref, path=path, context=int(context or 0)),
            parameters=params({
                "query": {"type": "string", "description": "Regex or literal, e.g. 'def _wear' or 'get_clean_equipments\\('; lower-case matches any case"},
                "project": _PROJECT_PARAM,
                "ref": {"type": "string", "description": "Branch, MR ('!169' or its URL) or commit sha; empty = default branch. For a review, the MR", "default": ""},
                "path": {"type": "string", "description": "Only under this path or glob, e.g. 'app/core/' or '*.py'", "default": ""},
                "context": {"type": "integer", "description": "Lines of context around each match (default 2, max 10)", "default": 2},
            }, ["query"]),
        ),
        Tool(
            name="get_file",
            description=(
                "Read a file at any ref, with line numbers. Returns as much as fits in one result, and "
                "the footer gives the start_line to continue from. To look at several places of one "
                "file, pass `ranges` ('120-180,540-560') and get them all in one call. A wrong path "
                "comes back with the closest real paths."),
            fn=lambda file_path, ref="", project="", start_line=1, num_lines=0, ranges="": get_file(
                file_path, ref, project=project, start_line=int(start_line or 1),
                num_lines=int(num_lines or 0), ranges=ranges),
            parameters=params({
                "file_path": {"type": "string", "description": "File path in the repo (e.g. 'app/core/decision/service.py')"},
                "ref": {"type": "string", "description": "Branch, MR ('!169' or its URL) or commit; empty = default branch", "default": ""},
                "project": _PROJECT_PARAM,
                "start_line": {"type": "integer", "description": "1-based line to start from (default 1)", "default": 1},
                "num_lines": {"type": "integer", "description": "How many lines to return; 0 (default) means as many as fit", "default": 0},
                "ranges": {"type": "string", "description": "Several windows at once, e.g. '120-180,540-560' (overrides start_line)", "default": ""},
            }, ["file_path"]),
        ),
        Tool(
            name="list_pipelines",
            description=(
                f"List recent CI/CD pipelines (default repo: {s.project_path}), "
                "optionally filtered by branch and status. First step of reading CI: "
                "pipeline → list_pipeline_jobs → get_job_log."
            ),
            fn=lambda project="", ref="", status="": gitlab_list_pipelines(
                project=project, ref=ref, status=status),
            parameters=params({
                "project": _PROJECT_PARAM,
                "ref": {"type": "string", "description": "Branch or tag to filter by (e.g. 'main', an MR source branch)"},
                "status": {"type": "string", "description": "Filter: failed, success, running, canceled, pending"},
            }),
        ),
        Tool(
            name="list_pipeline_jobs",
            description=(
                "List the jobs of one pipeline — stage, status, failure reason, duration and "
                "each job's id. Use after list_pipelines to find WHICH job failed; the job id "
                "printed here is what get_job_log takes."
            ),
            fn=lambda pipeline_id, project="", scope="": gitlab_list_pipeline_jobs(
                int(pipeline_id), project=project, scope=scope),
            parameters=params({
                "pipeline_id": {"type": "integer", "description": "Pipeline id from list_pipelines"},
                "project": _PROJECT_PARAM,
                "scope": {"type": "string", "description": "Only jobs in this state: failed, success, running, canceled, skipped, manual"},
            }, ["pipeline_id"]),
        ),
        Tool(
            name="get_job_log",
            description=(
                "Read a CI job's log (trace) to find out why it failed. Returns the END of the "
                "log by default (that is where the failure is) with ANSI codes and section "
                "markers stripped; the footer says how many earlier lines were not shown. For a "
                "long log, pass search='Traceback|error|FAILED' to pull just the matching lines "
                "with their line numbers. Get the job id from list_pipeline_jobs."
            ),
            fn=lambda job_id, project="", tail_lines=200, search="": gitlab_get_job_log(
                int(job_id), project=project, tail_lines=int(tail_lines or 200), search=search),
            parameters=params({
                "job_id": {"type": "integer", "description": "Job id from list_pipeline_jobs"},
                "project": _PROJECT_PARAM,
                "tail_lines": {"type": "integer", "description": "Lines from the end of the log (default 200)", "default": 200},
                "search": {"type": "string", "description": "Regex/keyword: return matching lines instead of the tail"},
            }, ["job_id"]),
        ),
        Tool(
            name="search_issues",
            description="Search GitLab issues",
            fn=gitlab_search_issues,
            parameters=params({
                "query": {"type": "string", "description": "Search keyword"},
                "project": _PROJECT_PARAM,
            }, ["query"]),
        ),
        # --- GitLab (review-only extras) ---
        Tool(
            name="get_mr_diff",
            description="Get the code diff (changes) of a specific MR",
            fn=lambda mr_iid, project="": gitlab_get_mr_diff(int(mr_iid), project=project),
            parameters=params({
                "mr_iid": {"type": "integer", "description": "MR number"},
                "project": _PROJECT_PARAM,
            }, ["mr_iid"]),
        ),
        Tool(
            name="search_mrs",
            description="Search Merge Requests by title, branch name, or description keyword",
            fn=gitlab_search_mrs,
            parameters=params({
                "query": {"type": "string", "description": "Search term (branch name, ticket number, keyword)"},
                "state": {"type": "string", "description": "Filter: opened, merged, closed, all (default: all)"},
                "project": _PROJECT_PARAM,
            }, ["query"]),
        ),
        Tool(
            name="comment_mr",
            description="Post a general comment (note) on a GitLab Merge Request (not tied to a specific line)",
            fn=lambda mr_iid, body, project="": gitlab_comment_mr(int(mr_iid), body, project=project),
            parameters=params({
                "mr_iid": {"type": "integer", "description": "MR number (e.g. 73)"},
                "body": {"type": "string", "description": "Comment content in Markdown"},
                "project": _PROJECT_PARAM,
            }, ["mr_iid", "body"]),
            writes=True,
        ),
        Tool(
            name="reply_mr_comment",
            description=(
                "Reply in an existing comment thread on a GitLab Merge Request (inline or general), "
                "optionally resolving it. Take the thread id from get_mr_comments (\"thread <id>\")."
            ),
            fn=lambda mr_iid, discussion_id, body, resolve=False, project="": gitlab_reply_mr_comment(
                int(mr_iid), discussion_id, body, resolve=bool(resolve), project=project),
            parameters=params({
                "mr_iid": {"type": "integer", "description": "MR number (e.g. 73)"},
                "discussion_id": {"type": "string", "description": "The thread's id from get_mr_comments"},
                "body": {"type": "string", "description": "Reply content in Markdown"},
                "resolve": {"type": "boolean", "description": "Also mark the thread resolved", "default": False},
                "project": _PROJECT_PARAM,
            }, ["mr_iid", "discussion_id", "body"]),
            writes=True,
        ),
        Tool(
            name="update_mr",
            description=(
                "Edit an open MR's title, description, or target branch. Cannot close, "
                "merge, or reassign. Use append_description=True to add to the existing "
                "description instead of replacing it."
            ),
            fn=lambda mr_iid, title="", description="", append_description=False, target_branch="", project="": gitlab_update_mr(
                int(mr_iid), title=title, description=description,
                append_description=bool(append_description),
                target_branch=target_branch, project=project),
            parameters=params({
                "mr_iid": {"type": "integer", "description": "MR number (e.g. 73)"},
                "title": {"type": "string", "description": "New title (omit to leave unchanged)"},
                "description": {"type": "string", "description": "New description in Markdown (omit to leave unchanged)"},
                "append_description": {"type": "boolean", "description": "Append to the current description instead of replacing it", "default": False},
                "target_branch": {"type": "string", "description": "New target branch (omit to leave unchanged)"},
                "project": _PROJECT_PARAM,
            }, ["mr_iid"]),
            writes=True,
        ),
        Tool(
            name="create_mr",
            description=(
                "Open a Merge Request from a branch that is ALREADY pushed to GitLab. "
                "Does not create the branch or push code — push is a human step."
            ),
            fn=lambda source_branch, title, description="", target_branch="", project="": gitlab_create_mr(
                source_branch, title, description=description,
                target_branch=target_branch, project=project),
            parameters=params({
                "source_branch": {"type": "string", "description": "Branch with the changes; must already exist on GitLab"},
                "title": {"type": "string", "description": "MR title"},
                "description": {"type": "string", "description": "MR description in Markdown"},
                "target_branch": {"type": "string", "description": "Branch to merge into (default: the repo's default branch)"},
                "project": _PROJECT_PARAM,
            }, ["source_branch", "title"]),
            writes=True,
        ),
        Tool(
            name="comment_mr_lines",
            description=(
                "Post inline review comments on lines of an MR's diff — every comment of a review in "
                "ONE call; they are posted in parallel and each one's outcome is listed. A line that is "
                "not in the diff is posted as a general comment prefixed with file:line."),
            fn=lambda mr_iid, comments, project="": gitlab_comment_mr_lines(int(mr_iid), comments, project=project),
            parameters=params({
                "mr_iid": {"type": "integer", "description": "MR number (e.g. 73)"},
                "comments": {"type": "array", "description": "One item per issue: {file_path, new_line, body}", "items": {
                    "type": "object", "properties": {
                        "file_path": {"type": "string", "description": "File path in the diff (e.g. 'src/main.py')"},
                        "new_line": {"type": "integer", "description": "Line number in the new version of the file"},
                        "body": {"type": "string", "description": "Comment content in Markdown"}},
                    "required": ["file_path", "new_line", "body"]}},
                "project": _PROJECT_PARAM,
            }, ["mr_iid", "comments"]),
            writes=True,
        ),
        # --- source: what arrived on its own (meeseek's trigger loop) ---
        Tool("mr_poll", "Reviews asked of you and comments on your MRs (a trigger source).", mr_poll,
             params({"since": {"type": "number"}, "state": {"type": "object"}}), source=True),
    ]


def placeholders() -> dict[str, str]:
    s = settings()
    return {"gitlab_project_path": s.project_path,
            "local_clone": s.local_clone or "(not set: mcp_servers.gitlab.config.local_clone)"}
