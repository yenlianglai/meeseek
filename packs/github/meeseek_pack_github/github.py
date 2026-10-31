"""GitHub's REST API, the parts a team asks about: issues, pull requests and
their discussions, and a comment on one.

Settings (project.yaml `mcp_servers.github.config`):
    repos      the repositories the tools may reach, "owner/name"; the first is
               the default. Empty: any public repository, the default none.
    api_url    https://api.github.com, or a GitHub Enterprise server's /api/v3
Secret: GITHUB_TOKEN — optional for public repositories (a lower rate limit
without it); needed for private ones and to comment.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from meeseek_pack_kit import config, http, secret

_MAX_PATCH_CHARS = 12_000
_MAX_BODY_CHARS = 2_000


def repos() -> list[str]:
    return [str(r) for r in config().get("repos") or []]


def _api() -> str:
    return str(config().get("api_url") or "https://api.github.com").rstrip("/")


def _repo(repo: str) -> str:
    """The repository to ask: the one named, if allowed, else the default."""
    allowed = repos()
    repo = (repo or (allowed[0] if allowed else "")).strip()
    if not repo:
        raise ValueError("name a repository: owner/name")
    if allowed and repo not in allowed:
        raise ValueError(f"'{repo}' is not one of the configured repositories: {', '.join(allowed)}")
    return repo


def _call(path: str, *, method: str = "GET", body: dict | None = None) -> dict | list:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "meeseek-pack-github"}
    if token := secret("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    return http.request(f"{_api()}/{path}", method=method, headers=headers, body=body)


def _error(data) -> str | None:
    return f"GitHub API error: {data['error']}" if isinstance(data, dict) and "error" in data else None


def _line(item: dict) -> str:
    kind = "PR" if "pull_request" in item else "issue"
    who = (item.get("user") or {}).get("login", "?")
    labels = ", ".join(lb["name"] for lb in item.get("labels") or [])
    return (f"- #{item['number']} [{kind}, {item.get('state', '?')}] {item.get('title', '')} — {who}"
            + (f" ({labels})" if labels else ""))


def search_issues(query: str, repo: str = "", state: str = "", max_results: int = 20) -> str:
    """Issues and pull requests matching `query` (GitHub search syntax)."""
    q = f"{query} repo:{_repo(repo)}" + (f" state:{state}" if state else "")
    data = _call(f"search/issues?q={quote_plus(q)}&per_page={min(int(max_results), 50)}")
    if err := _error(data):
        return err
    items = data.get("items") or []
    if not items:
        return f"Nothing matches `{q}`."
    return f"**{data.get('total_count', len(items))} found** for `{q}`:\n" + "\n".join(_line(i) for i in items)


def get_issue(number: int, repo: str = "") -> str:
    """An issue or pull request, its description and its latest comments."""
    repo = _repo(repo)
    item = _call(f"repos/{repo}/issues/{int(number)}")
    if err := _error(item):
        return err
    comments = _call(f"repos/{repo}/issues/{int(number)}/comments?per_page=100")
    out = [_line(item), f"URL: {item.get('html_url', '')}", "", (item.get("body") or "(no description)")[:_MAX_BODY_CHARS]]
    if isinstance(comments, list) and comments:
        out.append(f"\n## Comments ({len(comments)}, latest 10)")
        for c in comments[-10:]:
            out.append(f"- {(c.get('user') or {}).get('login', '?')} at {c.get('created_at', '')[:16]}: "
                       f"{(c.get('body') or '')[:500]}")
    return "\n".join(out)


def get_pull_request(number: int, repo: str = "") -> str:
    """A pull request: its branches, its review state, and the diff of each file."""
    repo = _repo(repo)
    pr = _call(f"repos/{repo}/pulls/{int(number)}")
    if err := _error(pr):
        return err
    files = _call(f"repos/{repo}/pulls/{int(number)}/files?per_page=100")
    head = (f"#{pr['number']} {pr.get('title', '')} — {pr.get('state')}"
            f"{', merged' if pr.get('merged') else ''}{', draft' if pr.get('draft') else ''}\n"
            f"{(pr.get('head') or {}).get('ref', '?')} → {(pr.get('base') or {}).get('ref', '?')}, "
            f"+{pr.get('additions', 0)} −{pr.get('deletions', 0)} in {pr.get('changed_files', 0)} file(s)\n"
            f"URL: {pr.get('html_url', '')}")
    if not isinstance(files, list):
        return head + f"\n\n(files unreadable: {_error(files)})"
    parts, used = [head], 0
    for f in files:
        patch = f.get("patch") or "(binary or too large to show)"
        if used + len(patch) > _MAX_PATCH_CHARS:
            parts.append(f"\n… {len(files) - len(parts) + 1} more file(s) not shown (diff over {_MAX_PATCH_CHARS} chars)")
            break
        used += len(patch)
        parts.append(f"\n### {f['filename']} ({f.get('status')}, +{f.get('additions', 0)} −{f.get('deletions', 0)})"
                     f"\n```diff\n{patch}\n```")
    return "\n".join(parts)


def comment(number: int, body: str, repo: str = "") -> str:
    """Post a comment on an issue or pull request."""
    if not body.strip():
        return "Failed to comment: the body is empty."
    repo = _repo(repo)
    out = _call(f"repos/{repo}/issues/{int(number)}/comments", method="POST", body={"body": body})
    if err := _error(out):
        return f"Failed to comment on #{number}: {err}"
    return f"Comment posted on #{number}: {out.get('html_url', '')}"
