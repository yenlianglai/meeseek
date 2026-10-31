"""Jira: search, read and sprint tickets (REST v3).

Settings (project.yaml `mcp_servers.jira.config`):
    base_url        https://<site>.atlassian.net
    projects        [{key, board, component?}]; the first is the default
    ticket_prefix   the summary prefix sprint tickets carry, e.g. "[Team]"
Secrets: JIRA_USERNAME, JIRA_API_TOKEN.
"""

import base64
from urllib.parse import quote_plus

from meeseek_pack_kit import config, http, secret


def _settings() -> dict:
    s = config()
    s.setdefault("base_url", "")
    return s


def project() -> dict:
    """The default project: {key, board, component}."""
    projects = _settings().get("projects") or [{}]
    first = projects[0] if isinstance(projects[0], dict) else {"key": str(projects[0])}
    return {"key": first.get("key", ""), "board": first.get("board", ""), "component": first.get("component", "")}


def ticket_prefix() -> str:
    return str(_settings().get("ticket_prefix", "")).strip("[]")


def _jira_auth_header() -> str:
    credentials = f"{secret('JIRA_USERNAME')}:{secret('JIRA_API_TOKEN')}"
    return "Basic " + base64.b64encode(credentials.encode()).decode()


def _jira_get(endpoint: str) -> dict | list:
    """GET the Jira REST API (v3)."""
    return http.request(f"{_settings()['base_url']}/rest/api/3/{endpoint}", timeout=20, headers={
        "Authorization": _jira_auth_header(), "Accept": "application/json"})


def jira_search(jql: str, fields: str = "summary,status,assignee,duedate,priority,issuetype,parent", max_results: int = 30) -> str:
    """Search Jira tickets with JQL.

    Args:
        jql: JQL query string, OR free text (auto-wrapped as a text search)
        fields: Comma-separated fields to return
        max_results: Maximum number of results
    """
    # 沒有 JQL 運算子的自由文字包成 text search，否則 Jira 回 400。
    _JQL_MARKERS = ("=", "~", "<", ">", " in ", " in(", "order by", " is ", " was ", "!=")
    low = f" {jql.lower()} "
    if not any(m in low for m in _JQL_MARKERS):
        safe = jql.replace('"', '\\"')
        jql = f'text ~ "{safe}" ORDER BY updated DESC'
    params = f"jql={quote_plus(jql)}&fields={fields}&maxResults={max_results}"
    data = _jira_get(f"search/jql?{params}")

    if isinstance(data, dict) and "error" in data:
        return f"Jira API error: {data['error']}"

    issues = data.get("issues", [])
    if not issues:
        return f"No issues found for JQL: `{jql}`"

    lines = [f"**{len(issues)} issues found:**\n"]
    for issue in issues:
        key = issue["key"]
        fields_data = issue.get("fields", {})
        summary = fields_data.get("summary", "")
        status = fields_data.get("status", {}).get("name", "?")
        assignee = fields_data.get("assignee")
        assignee_name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"
        duedate = fields_data.get("duedate") or "—"
        priority = fields_data.get("priority", {}).get("name", "?")
        issue_type = fields_data.get("issuetype", {}).get("name", "?")

        lines.append(f"- **{key}**: {summary}")
        lines.append(f"  - Type: {issue_type} | Status: {status} | Priority: {priority}")
        lines.append(f"  - Assignee: {assignee_name} | Due: {duedate}")

    return "\n".join(lines)


def jira_get_issue(issue_key: str) -> str:
    """Get detailed info for a specific Jira issue.

    Args:
        issue_key: Issue key (e.g. PROJ-123)
    """
    data = _jira_get(f"issue/{issue_key}?fields=summary,status,assignee,duedate,priority,description,issuetype,parent,issuelinks,comment")

    if isinstance(data, dict) and "error" in data:
        return f"Jira API error: {data['error']}"

    fields_data = data.get("fields", {})
    summary = fields_data.get("summary", "")
    status = fields_data.get("status", {}).get("name", "?")
    assignee = fields_data.get("assignee")
    assignee_name = assignee.get("displayName", "Unassigned") if assignee else "Unassigned"
    duedate = fields_data.get("duedate") or "—"
    priority = fields_data.get("priority", {}).get("name", "?")
    issue_type = fields_data.get("issuetype", {}).get("name", "?")
    parent = fields_data.get("parent")
    parent_str = f"{parent['key']}: {parent['fields']['summary']}" if parent else "—"

    desc = fields_data.get("description")
    desc_text = ""
    if desc and isinstance(desc, dict):
        for block in desc.get("content", []):
            for item in block.get("content", []):
                if item.get("type") == "text":
                    desc_text += item.get("text", "")
            desc_text += "\n"
    desc_text = desc_text[:800] if desc_text else "N/A"

    links = fields_data.get("issuelinks", [])
    link_lines = []
    for link in links[:5]:
        link_type = link.get("type", {}).get("outward", "relates to")
        linked = link.get("outwardIssue") or link.get("inwardIssue")
        if linked:
            link_lines.append(f"  - {link_type} {linked['key']}: {linked['fields']['summary']}")

    comments = fields_data.get("comment", {}).get("comments", [])
    recent_comments = comments[-3:] if comments else []
    comment_lines = []
    for c in recent_comments:
        author = c.get("author", {}).get("displayName", "?")
        created = c.get("created", "")[:10]
        body_blocks = c.get("body", {}).get("content", [])
        body_text = ""
        for block in body_blocks:
            for item in block.get("content", []):
                if item.get("type") == "text":
                    body_text += item.get("text", "")
        comment_lines.append(f"  - [{created}] {author}: {body_text[:200]}")

    result = [
        f"**{data['key']}: {summary}**",
        f"- Type: {issue_type} | Status: {status} | Priority: {priority}",
        f"- Assignee: {assignee_name} | Due: {duedate}",
        f"- Parent: {parent_str}",
        f"- Description:\n  {desc_text}",
    ]
    if link_lines:
        result.append("- Links:\n" + "\n".join(link_lines))
    if comment_lines:
        result.append("- Recent comments:\n" + "\n".join(comment_lines))

    return "\n".join(result)


def jira_get_sprint_issues(project_key: str = "", prefix: str = "") -> str:
    """Get all open sprint issues with a specific summary prefix.

    Args:
        project_key: Jira project key (uses config default if empty)
        prefix: Summary prefix to filter (uses config default if empty)
    """
    project_key = project_key or project()["key"]
    prefix = prefix or ticket_prefix()
    jql = f'project = {project_key} AND sprint in openSprints() AND summary ~ "{prefix}" ORDER BY assignee, duedate'
    return jira_search(jql=jql, max_results=50)
