"""meeseek's GitHub pack — and the example of how a pack is written: plain
functions (github.py), each declared once as a Tool with its schema and what
it does to the world, served over MCP by the kit (`python -m meeseek_pack_github`)."""

from meeseek_pack_kit import Tool, params

from meeseek_pack_github import github


def tools() -> list[Tool]:
    allowed = github.repos()
    repo = {"type": "string", "description": "owner/name" + (f" (default {allowed[0]}; allowed: {', '.join(allowed)})"
                                                             if allowed else "")}
    number = {"type": "integer", "description": "the issue or pull request number"}
    return [
        Tool("github_search_issues", "Search a repository's issues and pull requests (GitHub search syntax: "
             "`is:pr is:open label:bug`, words in the title or body).",
             lambda query, repo="", state="", max_results=20: github.search_issues(query, repo, state, max_results),
             params({"query": {"type": "string"}, "repo": repo,
                     "state": {"type": "string", "description": "open or closed; empty = both"},
                     "max_results": {"type": "integer", "default": 20}}, ["query"])),
        Tool("github_get_issue", "Read an issue or pull request: its description and latest comments.",
             lambda number, repo="": github.get_issue(number, repo), params({"number": number, "repo": repo}, ["number"])),
        Tool("github_get_pull_request", "Read a pull request's branches, review state and per-file diff.",
             lambda number, repo="": github.get_pull_request(number, repo),
             params({"number": number, "repo": repo}, ["number"])),
        Tool("github_comment", "Post a comment on an issue or pull request. A write: a skill must be granted it.",
             lambda number, body, repo="": github.comment(number, body, repo),
             params({"number": number, "body": {"type": "string", "description": "Markdown"}, "repo": repo},
                    ["number", "body"]), writes=True),
    ]


def placeholders() -> dict[str, str]:
    allowed = github.repos()
    return {"github_repo": allowed[0] if allowed else "(no repository configured)"}
