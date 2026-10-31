"""meeseek's Jira pack: the tools, as an MCP server (`python -m meeseek_pack_jira`),
and `jira_poll`, a trigger source for the tickets that need you."""

from meeseek_pack_kit import Tool, params

from meeseek_pack_jira.jira import jira_get_issue, jira_get_sprint_issues, jira_search, project, ticket_prefix
from meeseek_pack_jira.sources import jira_poll


def tools() -> list[Tool]:
    key, prefix = project()["key"], ticket_prefix()
    return [
        Tool("jira_search", "Search Jira tickets with JQL",
             lambda jql, max_results=30: jira_search(jql=jql, max_results=int(max_results)),
             params({"jql": {"type": "string", "description": f"JQL query (e.g. project = {key} AND sprint in openSprints())"},
                     "max_results": {"type": "integer", "description": "Max results to return", "default": 30}},
                    ["jql"])),
        Tool("jira_get_issue", "Get detailed Jira issue info (description, comments, links)",
             lambda issue_key: jira_get_issue(issue_key=issue_key),
             params({"issue_key": {"type": "string", "description": f"Issue key (e.g. {key}-123)"}}, ["issue_key"])),
        Tool("jira_get_sprint_issues", f"Get current sprint tickets (prefix: {prefix})",
             lambda prefix="": jira_get_sprint_issues(prefix=prefix or ticket_prefix()),
             params({"prefix": {"type": "string", "description": "Summary prefix filter", "default": prefix}})),
        # --- source: what arrived on its own (meeseek's trigger loop) ---
        Tool("jira_poll", "Tickets assigned to you, mentions and others' comments (a trigger source).", jira_poll,
             params({"since": {"type": "number"}, "state": {"type": "object"}}), source=True),
    ]


def placeholders() -> dict[str, str]:
    """What a skill's instructions may name: {{jira_project_key}} and the rest."""
    p = project()
    return {"jira_project_key": p["key"], "board_name": p["board"], "jira_component": p["component"],
            "ticket_prefix": ticket_prefix()}
