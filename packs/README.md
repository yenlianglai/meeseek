# Packs

A pack gives the tools of one vendor (an issue tracker, a git host, a cloud) to meeseek.
Each pack is its own Python package and runs as its own MCP server.
Thus the core never imports a vendor.
Also, any other MCP client (Claude Code, an IDE) can run a pack.

| Pack | Tools | Settings (`config:`) | Secrets |
|---|---|---|---|
| [`jira`](jira/) | `jira_search`, `jira_get_issue`, `jira_get_sprint_issues` | `base_url`, `projects`, `ticket_prefix` | `JIRA_USERNAME`, `JIRA_API_TOKEN` |
| [`gitlab`](gitlab/) | MRs, pipelines, issues, `search_code` / `get_file` (a local git mirror), `review_context`; writes: `comment_mr`, `comment_mr_lines`, `reply_mr_comment`, `create_mr`, `update_mr` | `base_url`, `projects`, `skip_ssl_verify`, `local_clone` | `GITLAB_TOKEN` |
| [`google`](google/) | `search_gmail`, `get_gmail_thread`, `list_chat_spaces`, `get_chat_messages`; sources: `gmail_poll`, `chat_poll`, `calendar_poll`; internal: the Drive mirror, Google links for `fetch_url`, recordings and the calendar for the meetings | `folder_ids`, `extra_file_ids`, `exclude_file_ids`, `meetings_folder_id` | none: Application Default Credentials with the Workspace scopes (`python -m meeseek_pack_google.auth --check`) |
| [`github`](github/) | `github_search_issues`, `github_get_issue`, `github_get_pull_request`; write: `github_comment` — the example pack, on a public API | `repos`, `api_url` | `GITHUB_TOKEN` (optional for public repositories) |
| [`gcp`](gcp/) | `query_logs`, `k8s_status` (raw evidence), `list_alerts`, `get_alert_policy`; source: `alerts_poll` | `environments`, `default_namespace`, `project` | none: Application Default Credentials |

[`kit`](kit/) contains the parts of a pack. It is not a pack.

## Turning one on

```yaml
# project.yaml
mcp_servers:
  jira:
    pack: jira            # runs `python -m meeseek_pack_jira`
    config: {...}         # handed to the pack as JSON in MEESEEK_PACK_CONFIG
```

```bash
uv sync                  # installs every pack in packs/ beside meeseek (the default `packs` group)
```

- The read tools of a pack go to all skills.
- The write tools of a pack go only to the skills whose SKILL.md `tools:` grant them.
- When the pack runs, the MCP server of meeseek also gives the read tools to its clients.

## Writing one

Read [`github`](github/) first. It has four tools on a public API, and one of them is a write.

```python
from meeseek_pack_kit import Tool, config, params, serve

def get_issue(issue_key: str) -> str:
    ...

def tools() -> list[Tool]:
    return [Tool("get_issue", "Read an issue.", get_issue,
                 params({"issue_key": {"type": "string"}}, ["issue_key"]))]

if __name__ == "__main__":
    serve("tracker", tools(), placeholders=lambda: {"tracker_project": config()["project"]})
```

A tool uses these flags:

| Flag | When to set it |
|---|---|
| `writes=True` | The tool changes something outside the machine. The reply must say the result, and a skill must have a grant for the tool. |
| `side_effect=True` | The tool changes only local files. |
| `shows_raw=True` | The result of the tool is raw evidence that the reply shows. |
| `source=True` | The tool is a trigger source. meeseek polls it for what arrived on its own. It is never a tool of an agent. |
| `internal=True` | Only the code of meeseek calls the tool, by name (`meeseek.runtime.packs`). |

`placeholders` are values that the instructions of a skill can name as `{{name}}`.

A pack imports the kit and the SDK of its vendor. It never imports `meeseek` (tests/unit/test_boundaries.py).

- The tests of a pack are in `packs/<name>/tests/`.
- To run them, use `uv run --extra dev pytest packs`.
- To run the tests of the core with no pack installed, use `uv run --no-group packs --extra dev pytest tests`.
