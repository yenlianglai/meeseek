# Workstreams

A workstream is one piece of work: a ticket, an MR or a branch. meeseek and
Claude Code share one context for it. A new conversation or a new Claude Code
session about the work reads that context. It does not start from nothing.

## Problem

Before workstreams, each conversation and each session kept its own context.

- In eight days (2026-10-25 to 11-02), meeseek opened 115 sessions.
- One ticket appeared in 43 meeseek sessions and 23 Claude Code sessions.
- One MR appeared in 35 meeseek sessions and 16 Claude Code sessions.
- Each new session read the MR and the ticket again. The person explained the
  same work again, or copied text from one tool to the other.

## Design

### The workstream

`runtime/workstreams.py` keeps one JSON file for each workstream. The file holds:

| Field | Content |
|---|---|
| `key`, `aliases` | The names of the work: a Jira key, an MR (`mr:<path>!<iid>`), a branch (`branch:<name>`) |
| `goal`, `state`, `next` | A short summary. A fast model writes it after each turn about the work |
| `decisions`, `questions` | The decisions so far, and the open questions |
| `events` | What happened: turns, Inbox news, merges. The latest 30 stay |
| `sessions`, `claude` | The meeseek conversations and the Claude Code sessions that worked on it |

A text that names more than two pieces of work attaches to none. A sprint
summary is about many pieces of work, not about one.

### Who reads it and who writes it

| Where | What happens |
|---|---|
| A meeseek turn (`engine.turn`) | A message that names one piece of work attaches its conversation. Each turn of an attached conversation carries the context. After the turn, the summary is updated |
| `ask_claude` | The brief starts with the context. With no session named, the workstream's Claude session in the same folder continues. When that session ends, its answer goes into the summary |
| The Inbox (`runtime/triggers.py`) | News on a ticket or an MR becomes an event on its workstream. News alone does not open a workstream. A merge closes the workstream |
| Plugins (`workstream_get`, `workstream_note`) | meeseek's agents and Claude Code read the context and add notes |
| Claude Code's prompt hook (`scripts/workstream_hook.py`) | When a prompt names the work, or the session runs on a branch that holds the ticket key, the hook adds the context to the prompt. It adds the same context again only after the context changes. It also attaches the session to the workstream |
| The console | The Work tab lists the workstreams, with the context and the linked conversations |

### Closing

A merged MR closes its workstream. The summary goes to the knowledge base
notes (`notes/workstreams/<id>.md`). New work on the same ticket starts a new
workstream.

### Events to Claude Code

`workstreams.route_to_claude` lists the event kinds that also continue the
workstream's Claude session: `merged`, `review`, `ticket`. The default is an
empty list. The session gets the event and the context. It does not push and it
does not write to team systems. It says what it would write, and the person
decides.

## Configuration

```yaml
workstreams:
  enabled: true
  summarize: true          # a fast-model summary after each turn on a workstream
  context_chars: 2500      # how much context a turn or a Claude brief carries
  route_to_claude: []      # merged, review, ticket
```

The hook goes in `~/.claude/settings.json`. The command is in the header of
`scripts/workstream_hook.py`.

## Limits

- A ticket that moves to Done does not close its workstream. Only a merged MR does.
- Each turn on a workstream costs one fast-model call for the summary. Set
  `summarize: false` to keep only the events.
- The plugins are on meeseek's MCP endpoint, which has no authentication. The
  server binds to 127.0.0.1 (SECURITY.md).
