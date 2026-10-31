"""GitLab REST API tools for the self-hosted instance."""

import re
import ssl
from urllib.parse import quote_plus

from meeseek_pack_kit import http, secret

from meeseek_pack_gitlab.settings import settings


def _resolve_project(project: str) -> str:
    """Resolve the requested repo against the project.yaml allowlist.

    With a personal access token the credential can reach every repo the
    user can — this is the guardrail that keeps the agent inside
    `gitlab.projects`. Raises so the tool-error feedback loop tells the
    model which repos ARE allowed.
    """
    project = project or settings().project_path
    if project not in settings().projects:
        raise ValueError(
            f"Repo '{project}' 不在 project.yaml 的 gitlab.projects 允許清單。"
            f"可用: {', '.join(settings().projects)}"
        )
    return project


_MR_URL = re.compile(r"https?://[^/\s]+/(?P<project>[^\s]+?)/-/merge_requests/(?P<iid>\d+)")


def split_ref(project: str, ref: str) -> tuple[str, str]:
    """(project, ref) with an MR URL in `ref` turned into its repo and "!iid":
    the URL names the repo, which keeps an IID off the same number elsewhere."""
    if m := _MR_URL.search(ref or ""):
        return _resolve_project(m.group("project")), f"!{m.group('iid')}"
    return _resolve_project(project), (ref or "").strip()


def parse_mr(mr: str, project: str = "") -> tuple[str, int]:
    """'165', '!165' or an MR URL → (project, iid)."""
    project, ref = split_ref(project, str(mr))
    if m := re.fullmatch(r"!?(\d+)", ref):
        return project, int(m.group(1))
    raise ValueError(f"'{mr}' is not an MR number or MR URL")


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if settings().skip_ssl_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _gitlab(method: str, endpoint: str, payload: dict | None = None, *,
            text: bool = False, timeout: int = 15):
    result = http.request(f"{settings().base_url}/api/v4/{endpoint}", method=method,
                          headers={"PRIVATE-TOKEN": secret("GITLAB_TOKEN")},
                          body=payload, context=_ssl_ctx(), timeout=timeout, text=text)
    if isinstance(result, dict) and re.search(r"<(!doctype\s+)?html", str(result.get("error", "")), re.I):
        # An HTML page, not the API's JSON: something in front of GitLab (the
        # VPN, a proxy) answered. Read as "no permission", it sent the agent
        # after the token (2026-10-16).
        code = str(result["error"]).split(":", 1)[0]
        result = {**result, "error": f"{code}: GitLab could not be reached through the network (an HTML page "
                                     "came back instead of the API — usually the VPN is off or dropped), "
                                     "not a token or permission problem. Try again once connected."}
    return result


def _gitlab_get(endpoint: str) -> dict | list:
    return _gitlab("GET", endpoint)


def _gitlab_post(endpoint: str, payload: dict) -> dict:
    return _gitlab("POST", endpoint, payload)


def _gitlab_put(endpoint: str, payload: dict) -> dict:
    return _gitlab("PUT", endpoint, payload)


def _gitlab_get_text(endpoint: str) -> str | dict:
    """GET a plain-text endpoint (a job trace). A long build's trace is megabytes,
    hence the longer timeout."""
    return _gitlab("GET", endpoint, text=True, timeout=60)


def _post_error(result: dict) -> str | None:
    """User-facing error string if a _gitlab_post result is an error, else None.

    A write that fails (e.g. missing `api` scope → 403) must NOT be reported as
    success. On 401/403 we point at the token scope, since that is the common
    cause: MR notes/discussions are REST writes and require the `api` scope
    (`read_api` + `write_repository` are NOT enough — write_repository only
    covers git push, read_api is read-only).
    """
    if not (isinstance(result, dict) and "error" in result):
        return None
    err = str(result["error"])
    hint = ""
    if "403" in err or "401" in err:
        hint = (" — the token likely lacks the `api` scope. MR comments are REST "
                "writes; `read_api`/`write_repository` do not grant them. Re-issue "
                "the GitLab token with the `api` scope.")
    return f"{err}{hint}"


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _resolve_diff_position(diff_text: str, new_line: int) -> dict | None:
    """Map a target new-side line number to a commentable GitLab text position.

    GitLab rejects (HTTP 400) a `text` position whose `new_line` isn't a line the
    diff actually carries at that new-side number. Walking the diff hunks tells us
    the truth for each line:
      - added line ('+')      → {"new_line": N}
      - unchanged context (' ')→ {"new_line": N, "old_line": O}  (both required)
      - the target isn't in the diff at all → None (caller falls back)
    A deleted line ('-') has no new-side number, so it can't match `new_line`.
    """
    if not diff_text:
        return None
    old_ln = new_ln = 0
    for line in diff_text.split("\n"):
        m = _HUNK_RE.match(line)
        if m:
            old_ln = int(m.group(1))
            new_ln = int(m.group(2))
            continue
        if not line or line[0] == "\\":  # "\ No newline at end of file"
            continue
        tag = line[0]
        if tag == "+":
            if new_ln == new_line:
                return {"new_line": new_ln}
            new_ln += 1
        elif tag == "-":
            old_ln += 1
        else:  # context line (leading space)
            if new_ln == new_line:
                return {"new_line": new_ln, "old_line": old_ln}
            old_ln += 1
            new_ln += 1
    return None


def gitlab_list_open_mrs(project: str = "") -> str:
    """List open Merge Requests in the GitLab project.

    Args:
        project: GitLab project path (uses config default if empty)
    """
    project = _resolve_project(project)
    pid = quote_plus(project)
    mrs = _gitlab_get(f"projects/{pid}/merge_requests?state=opened&per_page=10&order_by=updated_at")
    if isinstance(mrs, dict) and "error" in mrs:
        return f"GitLab API error: {mrs['error']}"
    if not mrs:
        return f"No open MRs in {project}"
    lines = [f"**{len(mrs)} open MRs in {project}:**\n"]
    for mr in mrs:
        lines.append(f"- !{mr['iid']}: {mr['title']} (by {mr.get('author', {}).get('name', '?')})")
    return "\n".join(lines)


# An MR description or a comment comes back whole up to this size. Above it, the
# text ends with a "[truncated: N more characters]" marker and where to read the rest.
_MAX_TEXT_CHARS = 30_000


def _whole_text(text: str, rest: str) -> str:
    """Return `text` whole, or clipped at `_MAX_TEXT_CHARS` with a marker that says
    how much is missing and where (`rest`) to read it."""
    if len(text) <= _MAX_TEXT_CHARS:
        return text
    return (f"{text[:_MAX_TEXT_CHARS]}\n[truncated: {len(text) - _MAX_TEXT_CHARS} more characters;"
            f" read the rest at {rest}]")


def gitlab_get_mr(mr_iid: int, project: str = "") -> str:
    """Get details of a specific Merge Request by its IID (number).

    Args:
        mr_iid: The MR number (e.g. 73)
        project: GitLab project path (uses config default if empty)
    """
    project = _resolve_project(project)
    pid = quote_plus(project)
    mr = _gitlab_get(f"projects/{pid}/merge_requests/{mr_iid}")
    if isinstance(mr, dict) and "error" in mr:
        return f"GitLab API error: {mr['error']}"
    return (
        f"**!{mr['iid']}: {mr['title']}**\n"
        f"- State: {mr['state']}\n"
        f"- Author: {mr.get('author', {}).get('name', '?')}\n"
        f"- Source: `{mr['source_branch']}` → `{mr['target_branch']}`\n"
        f"- Created: {mr['created_at'][:10]}\n"
        f"- Updated: {mr['updated_at'][:10]}\n"
        f"- Description:\n{_whole_text(mr.get('description') or 'N/A', mr.get('web_url') or f'!{mr_iid}')}"
    )


def gitlab_search_mrs(query: str, state: str = "all", project: str = "") -> str:
    """Search Merge Requests by title, branch name, or description keyword.

    Args:
        query: Search term (branch name, ticket number, keyword in title)
        state: Filter by state: 'opened', 'merged', 'closed', 'all' (default: all)
        project: GitLab project path (uses config default if empty)
    """
    project = _resolve_project(project)
    pid = quote_plus(project)
    mrs = _gitlab_get(
        f"projects/{pid}/merge_requests?search={quote_plus(query)}&state={state}&per_page=10&order_by=updated_at"
    )
    if isinstance(mrs, dict) and "error" in mrs:
        return f"GitLab API error: {mrs['error']}"
    if not mrs:
        return f"No MRs found matching '{query}' (state={state})"
    lines = [f"**{len(mrs)} MRs matching '{query}' (state={state}):**\n"]
    for mr in mrs:
        status = mr.get("state", "?")
        lines.append(f"- !{mr['iid']}: [{status}] {mr['title']} (branch: `{mr.get('source_branch', '?')}`)")
    return "\n".join(lines)


def gitlab_list_pipelines(project: str = "", ref: str = "", status: str = "") -> str:
    """List recent CI/CD pipelines for the project.

    Args:
        project: GitLab project path (uses config default if empty)
        ref: Only pipelines for this branch/tag (empty = any)
        status: Only pipelines in this status, e.g. 'failed', 'success',
            'running' (empty = any)
    """
    project = _resolve_project(project)
    pid = quote_plus(project)
    query = f"projects/{pid}/pipelines?per_page=10&order_by=updated_at"
    if ref:
        query += f"&ref={quote_plus(ref)}"
    if status:
        query += f"&status={quote_plus(status)}"
    pipelines = _gitlab_get(query)
    if isinstance(pipelines, dict) and "error" in pipelines:
        return f"GitLab API error: {pipelines['error']}"
    if not pipelines:
        scope = "".join([f" (ref: `{ref}`)" if ref else "",
                         f" (status: {status})" if status else ""])
        return f"No pipelines found in {project}{scope}"
    lines = [f"**Recent pipelines in {project}:**\n"]
    for p in pipelines:
        lines.append(f"- #{p['id']}: {p['status']} (ref: `{p['ref']}`, {p['updated_at'][:10]})")
    lines.append("\nUse list_pipeline_jobs(<pipeline id>) to see which job failed, "
                 "then get_job_log(<job id>) for its output.")
    return "\n".join(lines)


def _format_jobs(jobs: list, pipeline_id: int, project: str) -> str:
    """Render a pipeline's jobs, grouped by stage in execution order.

    Failed jobs carry their failure_reason (script_failure vs runner_system_failure
    vs timeout are different problems), and each line ends with the job id, since
    that is the argument get_job_log needs next.
    """
    if not jobs:
        return (f"Pipeline #{pipeline_id} in {project} has no jobs "
                "(it may not have started, or the ref has no CI config).")

    _ICON = {"success": "✅", "failed": "❌", "running": "🔄", "pending": "⏳",
             "canceled": "⛔", "skipped": "⤼", "manual": "✋", "created": "·"}
    by_stage: dict[str, list[dict]] = {}
    for j in jobs:
        by_stage.setdefault(j.get("stage", "?"), []).append(j)

    lines = [f"**Pipeline #{pipeline_id} jobs in {project}** ({len(jobs)} jobs):\n"]
    for stage, stage_jobs in by_stage.items():
        lines.append(f"### stage `{stage}`")
        for j in stage_jobs:
            status = j.get("status", "?")
            bits = [f"{_ICON.get(status, '?')} **{j.get('name', '?')}** — {status}"]
            if j.get("allow_failure") and status == "failed":
                bits.append("(allow_failure)")
            reason = j.get("failure_reason")
            if reason:
                bits.append(f"[{reason}]")
            duration = j.get("duration")
            if duration:
                bits.append(f"{float(duration):.0f}s")
            bits.append(f"→ job {j.get('id', '?')}")
            lines.append("- " + " ".join(bits))
        lines.append("")

    failed = [j for j in jobs if j.get("status") == "failed"]
    if failed:
        ids = ", ".join(str(j.get("id")) for j in failed)
        lines.append(f"Failed: read the output with get_job_log({ids.split(', ')[0]}) "
                     f"(all failed job ids: {ids}).")
    return "\n".join(lines)


def gitlab_list_pipeline_jobs(pipeline_id: int, project: str = "", scope: str = "") -> str:
    """List the jobs of one CI/CD pipeline, with status, stage and job id.

    Args:
        pipeline_id: Pipeline id from list_pipelines (e.g. 481234)
        project: GitLab project path (uses config default if empty)
        scope: Only jobs in this state — 'failed', 'success', 'running',
            'canceled', 'skipped', 'manual' (empty = all)
    """
    project = _resolve_project(project)
    pid = quote_plus(project)
    query = f"projects/{pid}/pipelines/{int(pipeline_id)}/jobs?per_page=100"
    if scope:
        query += f"&scope[]={quote_plus(scope)}"
    jobs = _gitlab_get(query)
    if isinstance(jobs, dict) and "error" in jobs:
        return f"GitLab API error: {jobs['error']}"
    return _format_jobs(jobs, int(pipeline_id), project)


# One result's worth of job log: under the callback-level cap on tool results,
# big enough that a failing pytest/build tail arrives whole.
_MAX_LOG_CHARS = 20_000

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# GitLab wraps every log section in `section_start:<epoch>:<name>\r<ansi>` markers.
_SECTION_RE = re.compile(r"^section_(?:start|end):\d+:\S*\s*$")


def _clean_trace(text: str) -> str:
    """Strip ANSI colour codes, section markers and \\r progress rewrites from a trace."""
    text = _ANSI_RE.sub("", text.replace("\r\n", "\n"))
    out: list[str] = []
    for line in text.split("\n"):
        # A \r-rewritten line (progress bar): only the final state matters.
        if "\r" in line:
            line = line.split("\r")[-1]
        if _SECTION_RE.match(line):
            continue
        out.append(line.rstrip())
    return "\n".join(out).strip("\n")


def _log_window(text: str, tail_lines: int, search: str) -> str:
    """Render the interesting part of a log: matching lines, else the tail.

    A plain read returns the tail (where the failure is) and says how many lines
    were skipped. `search` instead pulls every matching line with its number.
    """
    try:
        tail_lines = max(1, int(tail_lines or 200))
    except (TypeError, ValueError):
        tail_lines = 200

    lines = text.split("\n") if text else []
    total = len(lines)
    if not text:
        return "The job log is empty (the job may not have started yet)."

    if search:
        try:
            pattern = re.compile(search, re.IGNORECASE)
        except re.error as e:
            return f"Invalid search regex {search!r}: {e}"
        hits = [(i + 1, ln) for i, ln in enumerate(lines) if pattern.search(ln)]
        if not hits:
            return f"No line of the {total}-line log matches {search!r}."
        kept, used, clipped = [], 0, False
        for num, ln in hits:
            entry = f"{num}: {ln}"
            if used + len(entry) + 1 > _MAX_LOG_CHARS - 300:
                clipped = True
                break
            kept.append(entry)
            used += len(entry) + 1
        footer = f"[{len(kept)} of {len(hits)} lines matching {search!r}, log is {total} lines]"
        if clipped:
            footer += " More matches were dropped — narrow the search."
        return "```\n" + "\n".join(kept) + f"\n```\n{footer}"

    wanted = lines[-tail_lines:]
    # Trim from the front: the last lines are the ones that explain the failure.
    budget = _MAX_LOG_CHARS - 300
    used, start = 0, len(wanted)
    for i in range(len(wanted) - 1, -1, -1):
        if used + len(wanted[i]) + 1 > budget:
            break
        used += len(wanted[i]) + 1
        start = i
    kept = wanted[start:]
    clipped_line = False
    if not kept and wanted:
        # The last line alone exceeds the budget: clip it rather than return an
        # empty fence that reads as "no log".
        kept = [wanted[-1][:budget] + " …(line clipped)"]
        clipped_line = True
    first_num = total - len(kept) + 1
    footer = f"[log lines {first_num}-{total} of {total}]"
    if clipped_line:
        footer += " The last line was longer than the whole result limit and was clipped mid-line."
    elif first_num > 1:
        footer += (f" {first_num - 1} earlier lines not shown — raise tail_lines, or "
                   f"pass search='...' to find something specific in them.")
    return "```\n" + "\n".join(kept) + f"\n```\n{footer}"


def gitlab_get_job_log(
    job_id: int,
    project: str = "",
    tail_lines: int = 200,
    search: str = "",
) -> str:
    """Read a CI job's log (trace) — the tail by default, or lines matching a search.

    Args:
        job_id: Job id from list_pipeline_jobs (e.g. 1720981)
        project: GitLab project path (uses config default if empty)
        tail_lines: How many lines from the END of the log to return (default 200)
        search: Regex/keyword — return every matching line with its line number
            instead of the tail (e.g. 'error|failed|Traceback')
    """
    project = _resolve_project(project)
    pid = quote_plus(project)
    job_id = int(job_id)

    job = _gitlab_get(f"projects/{pid}/jobs/{job_id}")
    if isinstance(job, dict) and "error" in job:
        return f"GitLab API error reading job {job_id}: {job['error']}"

    trace = _gitlab_get_text(f"projects/{pid}/jobs/{job_id}/trace")
    if isinstance(trace, dict):
        # 404 here means the job exists but has no trace (never ran, or expired).
        if "404" in str(trace["error"]):
            return (f"Job {job_id} (`{job.get('name', '?')}`, {job.get('status', '?')}) "
                    "has no log — it has not started, or its log has expired.")
        return f"GitLab API error reading job {job_id} log: {trace['error']}"

    header = (
        f"**Job {job_id}: `{job.get('name', '?')}`** — {job.get('status', '?')}"
        f" (stage: {job.get('stage', '?')}, ref: `{job.get('ref', '?')}`,"
        f" pipeline #{(job.get('pipeline') or {}).get('id', '?')})"
    )
    if job.get("failure_reason"):
        header += f"\nFailure reason: {job['failure_reason']}"

    return f"{header}\n\n" + _log_window(_clean_trace(trace), tail_lines, search)


def gitlab_search_issues(query: str, project: str = "") -> str:
    """Search GitLab issues by keyword.

    Args:
        query: Search term
        project: GitLab project path (uses config default if empty)
    """
    project = _resolve_project(project)
    pid = quote_plus(project)
    issues = _gitlab_get(f"projects/{pid}/issues?search={quote_plus(query)}&per_page=5&state=opened")
    if isinstance(issues, dict) and "error" in issues:
        return f"GitLab API error: {issues['error']}"
    if not issues:
        return f"No open issues matching '{query}' in {project}"
    lines = [f"**Issues matching '{query}':**\n"]
    for i in issues:
        lines.append(f"- #{i['iid']}: {i['title']} ({i['state']}, labels: {i.get('labels', [])})")
    return "\n".join(lines)


def gitlab_get_file(
    file_path: str,
    ref: str = "main",
    project: str = "",
    start_line: int = 1,
    num_lines: int = 0,
) -> str:
    """Read a file from the GitLab repository, windowed by line.

    Args:
        file_path: Path to the file in the repo (e.g. "src/main.py")
        ref: Branch or commit ref (default: "main")
        project: GitLab project path (uses config default if empty)
        start_line: 1-based line to start from (default 1)
        num_lines: How many lines to return; 0 (default) means as many as fit
            in one result
    """
    project = _resolve_project(project)
    pid = quote_plus(project)
    encoded_path = quote_plus(file_path)
    result = _gitlab_get(f"projects/{pid}/repository/files/{encoded_path}?ref={ref}")
    if isinstance(result, dict) and "error" in result:
        return f"GitLab API error: {result['error']}"

    import base64
    content = result.get("content", "")
    encoding = result.get("encoding", "base64")
    if encoding == "base64":
        try:
            content = base64.b64decode(content).decode("utf-8")
        except Exception:
            return f"Failed to decode file content for `{file_path}`"

    file_name = result.get("file_name", file_path)
    size = result.get("size", 0)
    return _file_window(content, file_name, file_path, ref, project, size,
                        start_line, num_lines)


# One result's worth of file content (same budget as kb_read): under the 50k
# callback-level cap on tool results while fitting a normal source file whole.
# Every clip says which lines came back and how to continue.
_MAX_FILE_CHARS = 24_000


def _file_window(
    content: str,
    file_name: str,
    file_path: str,
    ref: str,
    project: str,
    size: int,
    start_line: int,
    num_lines: int,
    numbered: bool = False,
) -> str:
    """Render the requested line window, plus how to read what is left. With
    `numbered`, each line carries its number — what a comment or a jump needs."""
    try:
        start_line = max(1, int(start_line))
        num_lines = max(0, int(num_lines))
    except (TypeError, ValueError):
        start_line, num_lines = 1, 0

    lines = content.splitlines()
    total = len(lines)
    if total == 0:
        return f"**`{file_name}`** (ref: {ref}, {size} bytes) — the file is empty."
    if start_line > total:
        return (f"**`{file_name}`** (ref: {ref}) has {total} lines; "
                f"start_line={start_line} is past the end.")

    wanted = lines[start_line - 1 : start_line - 1 + num_lines] if num_lines \
        else lines[start_line - 1 :]

    # Whole lines only; the footer's budget is reserved up front so it is never clipped.
    budget = _MAX_FILE_CHARS - 400
    kept: list[str] = []
    used = 0
    clipped_line = False
    if numbered:
        wanted = [f"{n:>5}  {line}" for n, line in enumerate(wanted, start_line)]
    for line in wanted:
        if used + len(line) + 1 > budget:
            if not kept:  # a single line longer than the whole budget
                kept.append(line[:budget] + " …(line clipped)")
                clipped_line = True
            break
        kept.append(line)
        used += len(line) + 1

    end = start_line + len(kept) - 1
    footer = f"[{file_path} @ {ref} — lines {start_line}-{end} of {total}, {size} bytes]"
    if clipped_line:
        footer += (" One line was longer than the whole result limit and was "
                   "clipped mid-line.")
    if end < total:
        proj = f", project='{project}'" if project else ""
        footer += (f" Not the whole file: continue with "
                   f"get_file('{file_path}', ref='{ref}'{proj}, start_line={end + 1}).")
    return f"**`{file_name}`** (ref: {ref}, {size} bytes)\n```\n" + "\n".join(kept) + f"\n```\n{footer}"


_changes_cache: dict[tuple[str, int], tuple[str, dict]] = {}


def _mr_changes(pid: str, mr_iid: int) -> dict:
    """The MR's /changes, cached per head commit: a large MR's diff takes seconds,
    and a review reads it once and then comments on it many times. The head
    commit is checked first (one light call), so a push is never answered from
    the cache."""
    mr = _gitlab_get(f"projects/{pid}/merge_requests/{mr_iid}")
    if isinstance(mr, dict) and "error" in mr:
        return mr
    head = mr.get("sha", "") if isinstance(mr, dict) else ""
    hit = _changes_cache.get((pid, int(mr_iid)))
    if head and hit and hit[0] == head:
        return hit[1]
    changes = _gitlab_get(f"projects/{pid}/merge_requests/{mr_iid}/changes")
    if not isinstance(changes, dict):
        return {"error": f"unexpected /changes response: {str(changes)[:200]}"}
    if "error" not in changes and head:
        if len(_changes_cache) > 50:
            _changes_cache.pop(next(iter(_changes_cache)))
        _changes_cache[(pid, int(mr_iid))] = (head, changes)
    return changes


def gitlab_get_mr_diff(mr_iid: int, project: str = "") -> str:
    """Get the code diff (changes) of a specific Merge Request.

    Args:
        mr_iid: The MR number (e.g. 73)
        project: GitLab project path (uses config default if empty)
    """
    project = _resolve_project(project)
    pid = quote_plus(project)
    changes = _mr_changes(pid, mr_iid)
    if "error" in changes:
        return f"GitLab API error: {changes['error']}"

    title = changes.get("title", "")
    diffs = changes.get("changes", [])
    if not diffs:
        return f"No changes found in !{mr_iid}"

    lines = [f"**!{mr_iid}: {title}** — {len(diffs)} files changed\n"]
    total_chars = 0
    max_chars = 12000

    for d in diffs:
        if total_chars > max_chars:
            lines.append(f"\n... ({len(diffs) - len(lines) + 1} more files truncated)")
            break
        path = d.get("new_path", d.get("old_path", "?"))
        diff_text = d.get("diff", "")[:2000]
        lines.append(f"### `{path}`")
        lines.append(f"```diff\n{diff_text}\n```")
        total_chars += len(diff_text)

    return "\n".join(lines)


def gitlab_get_mr_comments(mr_iid: int, project: str = "") -> str:
    """Get all comments (notes and discussions) on a Merge Request.

    Returns both general comments and inline code comments with their file/line
    context, each thread with the id `gitlab_reply_mr_comment` answers it by.

    Args:
        mr_iid: The MR number (e.g. 73)
        project: GitLab project path (uses config default if empty)
    """
    project = _resolve_project(project)
    pid = quote_plus(project)

    discussions = _gitlab_get(
        f"projects/{pid}/merge_requests/{mr_iid}/discussions?per_page=50"
    )

    if isinstance(discussions, dict) and "error" in discussions:
        return f"GitLab API error: {discussions['error']}"
    if not discussions:
        return f"No comments on !{mr_iid}"

    lines = [f"**Comments on !{mr_iid}** ({len(discussions)} threads):\n"]

    for disc in discussions:
        notes = disc.get("notes", [])
        if not notes:
            continue

        first_note = notes[0]
        if first_note.get("system", False):
            continue

        author = first_note.get("author", {}).get("name", "?")
        body = _whole_text(first_note.get("body", ""), f"thread {disc.get('id', '?')} of !{mr_iid} in GitLab")
        created = first_note.get("created_at", "")[:10]
        resolved = "✅" if disc.get("resolved", False) else ""
        thread = f"thread {disc.get('id', '?')}"

        position = first_note.get("position")
        if position:
            file_path = position.get("new_path", position.get("old_path", "?"))
            new_line = position.get("new_line", "?")
            lines.append(f"- **[{file_path}:{new_line}]** {resolved} ({author}, {created}) · {thread}")
        else:
            lines.append(f"- **General** {resolved} ({author}, {created}) · {thread}")

        lines.append(f"  > {body}")

        for reply in notes[1:]:
            if reply.get("system", False):
                continue
            r_author = reply.get("author", {}).get("name", "?")
            r_body = _whole_text(reply.get("body", ""), f"thread {disc.get('id', '?')} of !{mr_iid} in GitLab")
            lines.append(f"  ↳ **{r_author}**: {r_body}")

        lines.append("")

    return "\n".join(lines)


def gitlab_comment_mr(mr_iid: int, body: str, project: str = "") -> str:
    """Post a general comment (note) on a Merge Request.

    Args:
        mr_iid: The MR number (e.g. 73)
        body: Comment content in Markdown format
        project: GitLab project path (uses config default if empty)
    """
    project = _resolve_project(project)
    pid = quote_plus(project)
    try:
        result = _gitlab_post(
            f"projects/{pid}/merge_requests/{mr_iid}/notes",
            {"body": body},
        )
        err = _post_error(result)
        if err:
            return f"Failed to comment on !{mr_iid}: {err}"
        note_id = result.get("id", "?")
        return f"Comment posted on !{mr_iid} (note #{note_id})\nURL: {settings().base_url}/{project}/-/merge_requests/{mr_iid}#note_{note_id}"
    except Exception as e:
        return f"Failed to comment on !{mr_iid}: {e}"


def gitlab_reply_mr_comment(mr_iid: int, discussion_id: str, body: str, resolve: bool = False,
                            project: str = "") -> str:
    """Reply in a comment's thread on a Merge Request — an inline one or a
    general one (GitLab turns a lone comment into a thread when it is answered).

    Args:
        mr_iid: The MR number (e.g. 73)
        discussion_id: The thread's id, as gitlab_get_mr_comments lists it ("thread <id>")
        body: Reply content in Markdown format
        resolve: Also mark the thread resolved (an inline thread; a general comment cannot be)
        project: GitLab project path (uses config default if empty)
    """
    project = _resolve_project(project)
    pid = quote_plus(project)
    thread = f"projects/{pid}/merge_requests/{mr_iid}/discussions/{quote_plus(discussion_id.strip())}"
    try:
        result = _gitlab_post(f"{thread}/notes", {"body": body})
        err = _post_error(result)
        if err:
            if "404" in err:
                err += " — no such thread on this MR: take its id from get_mr_comments"
            return f"Failed to reply on !{mr_iid}: {err}"
        note_id = result.get("id", "?")
        out = (f"Reply posted on !{mr_iid} (note #{note_id})\n"
               f"URL: {settings().base_url}/{project}/-/merge_requests/{mr_iid}#note_{note_id}")
        if resolve:
            err = _post_error(_gitlab_put(thread, {"resolved": True}))
            out += f"\nThe thread was NOT resolved: {err}" if err else "\nThread resolved."
        return out
    except Exception as e:
        return f"Failed to reply on !{mr_iid}: {e}"


def _post_line_comment(project: str, pid: str, mr_iid: int, changes: dict,
                       file_path: str, new_line: int, body: str) -> tuple[bool, str]:
    """Post one inline comment against an already-fetched /changes. (ok, line)."""
    where = f"`{file_path}:{new_line}`"
    diff_refs = changes.get("diff_refs") or {}
    # GitLab 400s if new_line isn't an added/context line at that new-side
    # number — resolving against the actual hunks is what avoids that.
    diff_text = next(
        (d.get("diff", "") for d in changes.get("changes", [])
         if file_path in (d.get("new_path"), d.get("old_path"))),
        None,
    )
    line_pos = _resolve_diff_position(diff_text or "", new_line) if diff_text is not None else None
    try:
        if line_pos is None or not diff_refs:
            # The line isn't in the diff (unchanged/out-of-hunk) — GitLab can't
            # anchor there. Don't lose the feedback: post it as a general comment
            # with the file:line prefixed so the reviewer still sees the context.
            result = _gitlab_post(f"projects/{pid}/merge_requests/{mr_iid}/notes",
                                  {"body": f"**{where}** — {body}"})
            err = _post_error(result)
            if err:
                return False, f"{where}: FAILED — {err}"
            return True, (f"{where}: not in the diff (unchanged or out of hunk), posted as a "
                          f"general comment — {settings().base_url}/{project}/-/merge_requests/{mr_iid}#note_{result.get('id', '?')}")
        result = _gitlab_post(f"projects/{pid}/merge_requests/{mr_iid}/discussions", {
            "body": body,
            "position": {
                "position_type": "text",
                "base_sha": diff_refs["base_sha"],
                "head_sha": diff_refs["head_sha"],
                "start_sha": diff_refs["start_sha"],
                "new_path": file_path,
                "old_path": file_path,
                **line_pos,
            },
        })
        err = _post_error(result)
        if err:
            return False, f"{where}: FAILED — {err}"
        notes = result.get("notes") or [{}]
        return True, f"{where}: posted — {settings().base_url}/{project}/-/merge_requests/{mr_iid}#note_{notes[0].get('id', '?')}"
    except Exception as e:  # noqa: BLE001 — one comment's failure is reported, the rest still go
        return False, f"{where}: FAILED — {e}"


def gitlab_comment_mr_lines(mr_iid: int, comments: list[dict], project: str = "") -> str:
    """Post inline review comments on lines of a Merge Request's diff, all at once.

    The MR's diff is read once (cached per head commit) and the comments are
    posted in parallel; each one's outcome is reported on its own line.

    Args:
        mr_iid: The MR number (e.g. 73)
        comments: [{"file_path": "src/main.py", "new_line": 42, "body": "..."}]
        project: GitLab project path (uses config default if empty)
    """
    from concurrent.futures import ThreadPoolExecutor

    project = _resolve_project(project)
    pid = quote_plus(project)
    items, bad = [], []
    for i, c in enumerate(comments or []):
        try:
            items.append((str(c["file_path"]), int(c["new_line"]), str(c["body"])))
        except (KeyError, TypeError, ValueError):
            bad.append(f"comment {i + 1}: needs file_path, new_line and body")
    if bad or not items:
        return "Nothing was posted:\n- " + "\n- ".join(bad or ["no comments given"])
    changes = _mr_changes(pid, mr_iid)
    if "error" in changes:
        return f"Failed to post inline comments on !{mr_iid}: nothing posted — {changes['error']}"
    with ThreadPoolExecutor(max_workers=min(6, len(items)), thread_name_prefix="mr-comment") as pool:
        results = list(pool.map(
            lambda it: _post_line_comment(project, pid, mr_iid, changes, *it), items))
    failed = sum(1 for ok, _ in results if not ok)
    head = (f"{len(items) - failed} of {len(items)} comments posted on !{mr_iid}"
            + (f"; {failed} FAILED" if failed else "") + ":")
    return "\n".join([head, *(f"- {line}" for _, line in results)])


def _branch_exists(pid: str, branch: str) -> bool | dict:
    """True/False if `branch` exists on the remote, or an {"error": ...} dict.

    Only a 404 means "no such branch"; a connection failure is returned as an
    error, never as "branch missing".
    """
    result = _gitlab_get(f"projects/{pid}/repository/branches/{quote_plus(branch)}")
    if isinstance(result, dict) and "error" in result:
        if "404" in str(result["error"]):
            return False
        return result
    return True


def gitlab_create_mr(
    source_branch: str,
    title: str,
    description: str = "",
    target_branch: str = "",
    project: str = "",
) -> str:
    """Open a Merge Request from a branch that is ALREADY pushed to GitLab.

    Never creates a branch or pushes code: Claude Code commits on a local branch
    and a human pushes. If the branch is not on the remote yet, returns an
    error telling you to push it first.

    Args:
        source_branch: Branch holding the changes; must already exist on GitLab.
        title: MR title.
        description: MR description in Markdown (optional).
        target_branch: Branch to merge into (defaults to the repo's default branch).
        project: GitLab project path (uses config default if empty).
    """
    project = _resolve_project(project)
    pid = quote_plus(project)

    try:
        exists = _branch_exists(pid, source_branch)
        if isinstance(exists, dict):
            return f"Failed to create MR: could not check branch `{source_branch}`: {exists['error']}"
        if not exists:
            return (
                f"Branch `{source_branch}` does not exist on {project} — nothing to open an MR from. "
                "This tool never pushes code: commit on a local branch, then have a human push the "
                "branch, then call this again."
            )

        if not target_branch:
            info = _gitlab_get(f"projects/{pid}")
            if isinstance(info, dict) and "error" in info:
                return f"Failed to create MR: could not read {project}'s default branch: {info['error']}"
            target_branch = info.get("default_branch") or "main"

        if source_branch == target_branch:
            return f"Failed to create MR: source and target are both `{source_branch}`."

        payload = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
        }
        if description:
            payload["description"] = description

        result = _gitlab_post(f"projects/{pid}/merge_requests", payload)
        err = _post_error(result)
        if err:
            return f"Failed to create MR from `{source_branch}`: {err}"

        mr_iid = result.get("iid", "?")
        return (
            f"MR !{mr_iid} created: {result.get('title', title)}\n"
            f"- `{source_branch}` → `{target_branch}`\n"
            f"URL: {result.get('web_url', f'{settings().base_url}/{project}/-/merge_requests/{mr_iid}')}"
        )
    except Exception as e:
        return f"Failed to create MR from `{source_branch}`: {e}"


def gitlab_update_mr(
    mr_iid: int,
    title: str = "",
    description: str = "",
    append_description: bool = False,
    target_branch: str = "",
    project: str = "",
) -> str:
    """Edit an open Merge Request's title, description, or target branch.

    Cannot close, merge, reassign, or relabel an MR. `description` REPLACES the
    existing body; pass `append_description=True` to add to the end instead (the
    right mode for adding a test plan or review notes to someone else's MR).

    Args:
        mr_iid: The MR number (e.g. 73).
        title: New title (omit to leave unchanged).
        description: New description in Markdown (omit to leave unchanged).
        append_description: Append to the current description instead of replacing it.
        target_branch: New target branch (omit to leave unchanged).
        project: GitLab project path (uses config default if empty).
    """
    project = _resolve_project(project)
    pid = quote_plus(project)

    if not any((title, description, target_branch)):
        return (f"Nothing to update on !{mr_iid} — pass at least one of "
                "title, description, or target_branch.")
    if append_description and not description:
        return (f"append_description=True but no description text given for !{mr_iid} "
                "— pass the text to append.")

    try:
        payload: dict = {}
        if title:
            payload["title"] = title
        if target_branch:
            payload["target_branch"] = target_branch

        if description:
            if append_description:
                # Read-modify-write; a failed read refuses rather than replacing.
                current = _gitlab_get(f"projects/{pid}/merge_requests/{mr_iid}")
                if isinstance(current, dict) and "error" in current:
                    return (f"Failed to update !{mr_iid}: could not read its current "
                            f"description to append to: {current['error']}")
                existing = (current.get("description") or "").rstrip()
                payload["description"] = f"{existing}\n\n{description}" if existing else description
            else:
                payload["description"] = description

        result = _gitlab_put(f"projects/{pid}/merge_requests/{mr_iid}", payload)
        err = _post_error(result)
        if err:
            return f"Failed to update !{mr_iid}: {err}"

        changed = []
        if title:
            changed.append("title")
        if description:
            changed.append("description (appended)" if append_description else "description (replaced)")
        if target_branch:
            changed.append(f"target → `{target_branch}`")
        return (
            f"MR !{mr_iid} updated: {', '.join(changed)}\n"
            f"URL: {result.get('web_url', f'{settings().base_url}/{project}/-/merge_requests/{mr_iid}')}"
        )
    except Exception as e:
        return f"Failed to update !{mr_iid}: {e}"
