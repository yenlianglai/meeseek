"""A small MCP server over stdio, standing in for an issue tracker's."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("tracker")


@server.tool()
def jira_get_issue(issue_key: str) -> str:
    """Read an issue."""
    return f"{issue_key}: In Review"


@server.tool()
def jira_create_issue(summary: str) -> str:
    """Open an issue."""
    return f"created ACME-99: {summary}"


@server.tool()
def jira_delete_everything() -> str:
    """Not in the filter: never offered."""
    return "gone"


if __name__ == "__main__":
    server.run()
