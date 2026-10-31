"""review_context — everything a code review starts from, in one call.

A review always opened with the same reads, one model step each: the MR, its
diff, its discussions, its CI. They do not depend on each other, so code
fetches them side by side and the model starts from the whole picture. The
ticket it implements lives in another system (the issue tracker's pack), so
this names its key and the model reads it there.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus

_TICKET = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def review_context(mr: str, project: str = "") -> str:
    """The MR, its ticket's key, CI status, existing discussions and the diff —
    read in parallel."""
    from meeseek_pack_gitlab import gitlab

    try:
        project, iid = gitlab.parse_mr(mr, project)
    except ValueError as e:
        return f"Error: {e}"
    pid = quote_plus(project)
    meta = gitlab._gitlab_get(f"projects/{pid}/merge_requests/{iid}")
    if not isinstance(meta, dict) or "error" in meta:
        return f"Error: MR !{iid} in {project}: {meta.get('error') if isinstance(meta, dict) else meta}"

    ticket = next(iter(_TICKET.findall(f"{meta.get('title', '')} {meta.get('source_branch', '')}")), "")
    pipeline = meta.get("head_pipeline") or {}

    jobs = {
        "mr": lambda: gitlab.gitlab_get_mr(iid, project=project),
        "diff": lambda: gitlab.gitlab_get_mr_diff(iid, project=project),
        "discussions": lambda: gitlab.gitlab_get_mr_comments(iid, project=project),
    }
    if pipeline.get("status") == "failed" and pipeline.get("id"):
        jobs["ci"] = lambda: gitlab.gitlab_list_pipeline_jobs(int(pipeline["id"]), project=project,
                                                              scope="failed")
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="review-ctx") as pool:
        futures = {k: pool.submit(fn) for k, fn in jobs.items()}
        got = {}
        for k, f in futures.items():
            try:
                got[k] = f.result()
            except Exception as e:  # noqa: BLE001 — one missing part must not lose the rest
                got[k] = f"(could not read: {type(e).__name__}: {e})"

    ci = (f"pipeline #{pipeline.get('id')}: {pipeline.get('status')}"
          if pipeline else "no pipeline for the head commit")
    parts = [f"# Review context — !{iid} in {project}", got["mr"],
             f"## CI\n{ci}" + (f"\n{got['ci']}" if "ci" in got else "")]
    parts.append(f"## Ticket {ticket}\n(read its acceptance criteria with the issue tracker's tool)" if ticket
                 else "## Ticket\n(no ticket key in the title or branch)")
    parts += [f"## Discussions\n{got['discussions']}", f"## Diff\n{got['diff']}"]
    return "\n\n".join(parts)
