# MCP server — runtime/mcp_server.py: the server-level instructions an MCP host's model reads

Sections are read by name — `prompts.text("runtime/mcp_server#<section>", ...)`. A section runs from its `## name` line to the next one; the blank lines around it are not part of the text. This note is not sent to any model.

## instructions

meeseek — your context and agents. Hand it work that needs your own systems or knowledge — Jira, GitLab, Google Drive/Chat/Gmail, Kubernetes and GCP telemetry, the knowledge base — with ask_meeseek, rather than something you can do locally (rewording text, editing files in the current workspace). For a lookup, a picture or a check of a page you built, prefer the plugins — kb_*, jira_*, get_mr, search_code, query_logs, generate_image, render_diagram, review_page and the rest: one stateless call each, no agent turn. Results carry a `status`: `needs_input` means meeseek is asking you something — put the `needs_input` questions to your user and call ask_meeseek again with the same session_id; `running` means check_meeseek the task_id later. Files meeseek produces are listed by list_outputs and readable as outputs:// resources.
