# Security

## Reporting a vulnerability

Do not open a public issue for a security problem. Report it privately through the security advisory form of the repository ("Report a vulnerability" under the Security tab).
Include these items in the report:

- What you found.
- How to reproduce it.
- What it lets someone do.

You will get an answer within a week.

## What meeseek is, from a security point of view

meeseek is a personal assistant. You run it on your own machine, with your own credentials.
Its design makes it reachable by you, not by a network.

- **It binds to 127.0.0.1 by default.**
  - `meeseek serve --host 0.0.0.0` (or `HOST` for `scripts/serve.sh`) exposes the web console, its API and the MCP surface to your network **with no authentication**.
  - The MCP surface gives the MR writes of the GitLab pack directly (`mcp_server._DIRECT_WRITE_TOOLS`).
  - Anything that can reach the port can comment on MRs and edit MRs with your token.
  - Put authentication in front of the server before you expose it.
- **Writes wait for a person.**
  - A skill can use a tool that changes something outside the machine only when its `tools:` list grants that tool.
  - In a turn that nobody watches (the Inbox, the phone, an MCP caller), meeseek holds the write until you approve it.
  - meeseek writes rules and facts only after you said yes (`core/policies.py`: `ApprovalGate`, `Consent`).
- **Google is read-only at the credential layer.** The Google pack asks only for `.readonly` Workspace scopes (`packs/google/meeseek_pack_google/auth.py`).
- **Fetched pages cannot reach your network.**
  - `fetch_url` refuses each URL that resolves to a non-public address, and it follows no redirect (`tools/safe_fetch.py`).
  - meeseek serves the pages that an agent writes in a sandbox, with no network (`server.py`: `PAGE_CSP`).
- **Claude Code works in a folder.**
  - Work that meeseek gives to Claude Code runs in the folder that meeseek gives it.
  - meeseek refuses the writes of Claude Code outside that folder (`runtime/claude_agent.py`: `policy`).
  - meeseek also refuses each team-system write from Claude Code (`runtime/claude_agent.py`: `policy`).
- **Secrets stay local.**
  - `project.yaml`, `.env`, the knowledge base and `skills/*/learned.md` are gitignored.
  - A pack gets its settings from `project.yaml` and its secrets from the environment, never from a model.
