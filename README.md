# meeseek

meeseek is a multi-agent assistant for an engineering team. It runs on its own
small agent engine and can use any model: Gemini, Claude or an
OpenAI-compatible gateway. One orchestrator routes each request to a
specialist:

- research and the knowledge base
- code review and delivery
- infrastructure debugging
- Jira and sprints

The specialists work across Google Drive, GitLab, Jira/Confluence,
Kubernetes/GCP and Gmail/Chat.

The engine is general. All the data about one team is in `project.yaml`,
`.env` and the knowledge base. Thus you can point the engine at a different
stack and not change the code.

---

## Why this, beside Claude Code or Codex

Claude Code, Codex and similar tools are very good hands. A person opens a
session and points the tool at a repository. The tool works until the session
ends. meeseek is not a competitor to these hands, and it gives its heaviest
work to Claude Code. meeseek is the part that the hands do not keep: **the
team's context, owned by the team**.

- **The context is yours（context 自主）.** All the data that the agents know
  is in files that the team owns:

  - the knowledge base, synced from Drive
  - learned rules. Each rule keeps the conversation that it came from, and
    each rule gets a weekly review.
  - the team graph
  - remembered facts
  - meeting transcripts and notes
  - all past conversations, which you can search

  All of this data has versions. You can examine it and edit it on the Context
  page. It is not locked in the session memory of one vendor or in the chat
  history of one person.

  The models are configuration. Today the engine runs Gemini and gives
  execution to Claude Code. If you replace either one, the context stays. The
  context of a coding agent starts at the repository and ends with the
  session. The context of meeseek grows, gets reviews, and expires on a
  lifecycle.
- **It is on when nobody is.** A coding agent works while a person watches.
  meeseek watches the systems of the team without a person: mail, Chat
  mentions, alerts, the calendar and meetings. meeseek records, transcribes
  and coaches the meetings. It keeps promises as on-disk wakes and as tasks
  with goals, and these survive restarts. It sends a summary of the work that
  finished unseen to your phone.
- **The boundaries are code, not promises.** Each write to a team system
  waits for a yes on the Inbox. A reply can omit a failed write, or promise a
  follow-up that nothing schedules. The harness sends that reply back.
  Autonomous turns have a daily cap, a wall-clock limit and quiet hours. The
  guardrails of a personal coding agent are in the session of each person. The
  guardrails of meeseek belong to the team, are in one place, and have tests.
- **One assistant across the team's systems.** Jira, GitLab, GCP/K8s
  telemetry, Drive, Gmail and Chat are in one assistant. The shared
  configuration is in git. The answers are in the language of the team.
  meeseek records the cost for each agent and each model.

If the task is one repository and one person at a keyboard, use Claude Code
or Codex directly. meeseek also does this every day. These tools do not give a
team what meeseek keeps: the context, sovereign.

---

## Quick start

```bash
git clone <repo-url> meeseek && cd meeseek
./scripts/setup.sh          # deps, config files, Google login, health check
./scripts/serve.sh          # web console at http://localhost:8787/ui
```

**Setup with an agent.** Open a coding agent (Claude Code, Cursor, …) in the
clone. Paste this text:

> Set this repo up for me by following README.md → "Setup", step by step.
> Run every command yourself and check each step before the next. Stop and ask
> me for anything marked **(you)**. Never commit `project.yaml` or `.env`.

---

## Setup

For a person, the setup is three tasks after step 0 (the tools):

1. Run `./scripts/setup.sh`. Answer `y` and sign in to Google in the browser.
2. Put three values in `.env`: your Atlassian email, an Atlassian API token
   and a GitLab token (step 3).
3. Run `./scripts/serve.sh`. Open <http://localhost:8787/ui>. Type `/sync`.

**Without a clone.** You can also install meeseek as a command. Install the
packs that you want with it, in the same environment. Each pack is its own
package under `packs/`.

```bash
uv tool install ./ --with ./packs/kit --with ./packs/jira --with ./packs/gitlab --with ./packs/gcp --with ./packs/google
meeseek init          # ~/.meeseek: project.yaml, .env and the example skills (MEESEEK_HOME to put it elsewhere)
meeseek serve         # http://127.0.0.1:8787/ui — the console is built from a clone (npm run build --prefix frontend)
```

`meeseek serve` listens on 127.0.0.1. With `--host 0.0.0.0`, other machines
can connect to it, and to the MCP surface too, with no auth (SECURITY.md).

The steps below give the same procedure in detail, for an agent or for when a
step fails. You can safely run each step again. Each step ends with a check.
Steps with the mark **(you)** need a browser, a secret or a decision that only
the person can make. An agent can do all the other steps.

### 0. Prerequisites

| Tool | For | Check |
|------|-----|-------|
| macOS | all functions. Linux works without the meeting recorder | — |
| [uv](https://docs.astral.sh/uv/) | Python 3.11 and the dependencies | `uv --version` |
| [gcloud](https://cloud.google.com/sdk/docs/install) | the single Google login | `gcloud --version` |
| Node 22+ and npm | the build of the web console | `node --version` |
| [Claude Code](https://claude.com/claude-code), logged in | `claude:` gives work to it. Optional | `claude --version` |
| Xcode Command Line Tools | the meeting recorder only | `xcode-select -p` |

To install a missing tool, use one of these commands:

- `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `brew install --cask google-cloud-sdk`
- `brew install node`
- `xcode-select --install`

**(you)** Your Google account must have these roles. If a check below fails,
ask the person who administers the GCP project:

- On `google_cloud.project` and `quota_project`: Vertex AI User
  (`roles/aiplatform.user`), Service Usage Consumer
  (`roles/serviceusage.serviceUsageConsumer`).
- On the `gcp_project` of each GCP pack environment (`mcp_servers.gcp.config.environments`): Logging Viewer, Monitoring
  Viewer, Kubernetes Engine Viewer (`roles/logging.viewer`,
  `roles/monitoring.viewer`, `roles/container.viewer`).

### 1. Install

```bash
./scripts/setup.sh
```

The script does these steps:

- It runs `uv sync`.
- It makes `project.yaml` (from `project.yaml.example`) and `.env` (from
  `.env.example`) if they are missing. It never overwrites them.
- It offers the Google login (step 4).
- It ends with `doctor.sh`.

It asks one yes/no question. An agent can do the same steps without the
question:

```bash
uv sync
[ -f project.yaml ] || cp project.yaml.example project.yaml
[ -f .env ] || cp .env.example .env
npm ci --prefix frontend
```

Check: `uv run python -c "import meeseek"` exits 0.

### 2. `project.yaml`

`project.yaml` starts as a copy of `project.yaml.example`. Fill in `project`
and `google_cloud`. Under `mcp_servers`, fill in the packs that you connect:
`jira`, `gitlab`, `gcp`, `google` (packs/README.md). If you do not include a
pack, its tools are also not available.

The comment of each key tells what the key holds. `config/schema.py` holds the
defaults. The per-person keys have the mark 「個人」 in the file, and they are
off by default. Turn on a key when you want its function:

| Key | Set to |
|-----|--------|
| `mcp_servers.gitlab.config.local_clone` | the absolute path to your clone of the primary repo (the folder that the code skill gives to Claude Code). Empty if you do not have one |
| `messengers.telegram`, `telegram_allowed_users` | Telegram on, with your user id (step 6) |
| `triggers.sources`, `record_meetings` | the sources that start work on their own: mail, Chat, alerts, calendar (each one is the source of a pack); meeting recording |
| `triggers.coach_meetings`, `coach_telegram_chat` | the meetings where the coach helps you, and your Telegram chat id |
| `mcp_servers.google.config.meetings_folder_id` | your Meet recordings folder, for `/meetings` |
| `claude.enabled` | `false` if you do not have a Claude Code seat |

A team that shares one setup can keep its `project.yaml` in a private repo of
its own, and copy the file in. This repo never contains a `project.yaml`.

Check: `uv run python -c "from meeseek.config import cfg; print(cfg.project.name)"`
prints the name of your project, with no warnings.

### 3. `.env` (you)

Put only secrets in this file:

| Variable | Where it comes from |
|----------|---------------------|
| `JIRA_USERNAME` | your Atlassian login email |
| `JIRA_API_TOKEN` | a token from <https://id.atlassian.com/manage-profile/security/api-tokens>. It is for both Jira and Confluence |
| `GITLAB_TOKEN` | GitLab → Preferences → Access tokens: `read_api` and `read_repository`, plus `api` to post MR comments |

Do not put other values in `.env`. The loader exports the Google settings, and
the Jira and Confluence URLs, from `project.yaml`.

Check: `./scripts/doctor.sh` shows `.env tokens filled`.

### 4. Google login (you)

```bash
gcloud auth application-default login \
  --client-id-file=client_secret.json \
  --scopes="$(uv run python -m meeseek_pack_google.auth --login-scopes)"
```

The login uses an OAuth client. Most company Workspaces block the gcloud
client for Gmail, Chat and Drive, and the browser shows "This app is blocked".
Thus, use the OAuth client of your org. Do these steps in a GCP project of the
org:

1. Set the audience of the OAuth consent screen to **Internal** (no review,
   no consent wall).
2. Make an OAuth client of type **Desktop app**.
3. Download its JSON.
4. Enable the Gmail, Google Chat and Google Drive APIs in that project.
5. Save the JSON as `client_secret.json` in the clone (gitignored).

One client is sufficient for the full team. A teammate who made a client can
give you the file. `setup.sh` shows you these steps: it links the console pages
for your project and gets the JSON from Downloads.

A browser opens. Sign in with your work account and allow every scope. A plain
`gcloud auth application-default login` is not sufficient. It does not have
the Workspace scopes, so Drive, Gmail and Chat answer 403.

Check: `uv run python -m meeseek_pack_google.auth --check` exits 0.

### 5. Verify

```bash
./scripts/doctor.sh
```

Every line shows ✓, and the script exits 0.

### 6. Run

```bash
./scripts/serve.sh          # builds the console when stale, then serves :8787
```

Open <http://localhost:8787/ui>. Type `/sync` to get the knowledge base from
Drive. After that, the knowledge base syncs every 30 minutes. Then try
`/plan`.

Optional, at any time later:

- **Claude Code as a client.** Sessions that you open in this repo get the
  meeseek tools from `.mcp.json`. For sessions in other folders, run
  `claude mcp add --transport http meeseek http://127.0.0.1:8787/mcp-server/mcp`.
- **Telegram.** Make a bot with @BotFather. Put `TELEGRAM_BOT_TOKEN` in
  `.env`. Set `messengers.telegram: true`. Restart meeseek. Send a message to
  the bot. The bot replies with your user id for `telegram_allowed_users`.
- **Meeting recording** (`triggers.record_meetings`). The first recording
  asks macOS for access to Screen & System Audio Recording, Microphone and
  Automation (Chrome).
- **Pinned notes.** Copy `knowledge_base/notes/examples/*.md` to
  `knowledge_base/notes/pinned/`. Edit the copies. The pinned notes go to
  every agent on every turn.

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| Drive, Gmail or Chat return 403; the console shows an auth banner | Do step 4 again, and allow every scope |
| meeseek replies ⛔ "沒有 GCP 專案 … 的使用權限" | Your account does not have the roles in step 0. Ask the admin of that project |
| A log, alert or pod query says that the account does not have an IAM permission | Get the viewer roles in step 0, on the project of that environment |
| meeseek replies 🔑 "Google 登入不存在或已失效" | Do step 4 again |
| The browser says "This app is blocked" during the Google login | Your Workspace blocks the gcloud client. Use the Desktop OAuth client of your org (step 4) |
| GitLab 401 or 404 | Make sure that the token has the correct scopes, and that the repo is in the `projects` of the GitLab pack |
| Jira 401 | Make sure that `JIRA_USERNAME` is the email that the token belongs to |
| The console is blank or out of date | Run `npm run build --prefix frontend` |
| `doctor.sh` says placeholder values | `project.yaml` still has the values of the example. Do step 2 |

---

## Design

The rules that the code follows, and the reasons for them.

1. **The engine is generic; the team is configuration.** `project.yaml` holds
   every non-secret setting. `config/schema.py` owns every default. The loader
   adds no defaults, and it gives a warning for each key that it does not
   know. `.env` holds only secrets. Team knowledge is in the knowledge base,
   not in prompts.

2. **Code decides what code can decide; the model judges.** If a given input
   has only one correct answer, code does the work. Examples:

   - make a Logging filter from an environment name (`query_logs`)
   - read in parallel all the data that a review starts from
     (`review_context`)
   - dispatch a slash command
   - report that a write failed

   The model does the work that needs judgment: what to examine, what it
   means, and what to answer. Each step that moves out of the model is one
   less call to pay for, and one less place where the model must guess.

3. **An agent gets three kinds of text, each in one place.**

   - Its instruction is its role (the prompt of the router, or the `SKILL.md`
     of a skill), plus `prompts/blocks/common.md`, plus the per-turn memory.
   - A rule about one tool is in the description of that tool. Thus the rule
     goes only to the agents that hold the tool.
   - Messages that the runtime injects into a turn are files in
     `src/meeseek/prompts/`. The runtime reads them by name.

   No other text is prompt.

4. **Reads are shared; writes are granted.** Every agent holds every
   read-only tool. If an agent does not have a read, it does not have a
   capability. A skill must declare each tool that changes something outside
   the process. `tests/unit/test_security_policy.py` records which skill can
   hold which tool.

5. **The reply must agree with what happened.** The session events of a turn
   contain its tool calls. Thus the code checks the tool calls, and does not
   trust them:

   - If the reply does not include a failed write, the harness sends the
     reply back to the model to report the failure (`runtime/harness.py`).
   - If the user asked for raw evidence (log lines, a pod table) and the reply
     omitted it, the evidence goes in front of the reply
     (`core/policies.py:Evidence`).

6. **A turn is state in its session.** A turn can stop because of a transient
   model error, a lost connection or a question to the user. The turn then
   continues from its last event. It does not start again:

   - The turn loop (`core/turn.py`) tries transient model failures again.
   - `TurnRunner.resume` continues a turn that stopped early (a restart, or a
     pause in the console). A read that the turn waited for runs again. A
     write gets the mark outcome-unknown, and meeseek does not send it again
     until a read has checked it (`ResumedWrites`).
   - If a request is larger than the context window of the model, meeseek
     trims older tool results and sends the request again.
   - `ask_user` pauses the turn until the answer arrives.

   The console, the MCP server and headless runs all use the same loop.

7. **Learn only with consent; a tool failure is a defect.** A correction
   becomes a rule only when the user agrees to it. Code enforces this: if a
   rule write or a fact write does not have a yes to an `ask_user` question,
   `runtime/consent.py` holds it. A rule keeps the conversation that it came
   from, so that meeseek can check the rule again. If a tool fails again and
   again, it is a bug that a person must fix in code. It is not a thing to
   teach the agents to work around.

8. **Measure before and after.** meeseek judges a change to a prompt, a tool
   or the loop with the harness eval and with the cost of real turns
   (`evals/`).

---

## How a request runs

```
 console / MCP host / headless job
              │
              ▼
 orchestrator ── slash command? ── run it in code
   │  vague and would change something? → ask_user, one question at a time
   │
   ├── research ──┐
   ├── code ──────┤  each: SKILL.md + common.md + memory,
   ├── infra ─────┤        every read tool, its granted writes,
   └── sprint ────┘        hand-off to a peer via transfer_to_agent
              │
              ▼
 plugins: harness (failed writes reported) · consent (rules and facts wait for a yes) · evidence (raw output shown)
```

**The focus gate.** Some requests make or change something (code, a ticket, a
document) and have a hole in them. A hole is, for example, several possible
meanings, no acceptance criteria, an unclear scope, or a bug without a repro.
The orchestrator clarifies such a request before it routes it. It asks one
`ask_user` question at a time, each with the answer that it recommends. Then it
gives a summary for you to confirm. Queries and specific requests go directly
to routing.

**Attachments.** You can attach images and PDFs to a message:

- PNG/JPEG/WebP/HEIC up to 5 MB
- PDF up to 10 MB
- 4 files and 12 MB per message

The agent that gets the turn sees the files. meeseek stores them with the
turn. Only the latest messages with files send their bytes again. The reason:
meeseek makes each request again from the events of the session. If it sent
all the files on each turn, the request would eventually be larger than the
request limit.

---

## Skills

Each directory under `skills/` is one routed sub-agent. It contains:

- `SKILL.md` (frontmatter plus the SOP)
- an optional `tools.py`
- `learned.md` (rules learned in conversation)

| Skill | What it does | Ships to |
|-------|--------------|----------|
| `research` | Research and brainstorms across the KB, code, Jira and the web; maintains the knowledge base (search, correct, sync); reads Gmail and Google Chat | `outputs/reports/` + KB notes |
| `code` | MR review with inline comments, and changes that Claude Code makes or verifies | `outputs/reviews/` + MR comments |
| `infra` | K8s and GCP debugging, read-only: pods, logs, metrics, traces, alerts | `outputs/debug_reports/` |
| `sprint` | Jira tickets and sprint health; Confluence pages | `outputs/sprint_reports/` |

The morning plan (`/plan`) is not a skill. Its collection is fixed, so it is
code plus one synthesis turn (`runtime/daily_plan.py`). It runs as an
orchestrator tool.

---

## Tools that do the work in code

- **Code reads** — `search_code` and `get_file` read a host-side shallow git
  mirror of each allowlisted repo (`packs/gitlab/meeseek_pack_gitlab/git_mirror.py`).
  They read at any ref: a branch, an MR (`!165` or its URL) or a commit. A
  search uses `git grep`, with line numbers and context. A read has line
  numbers and can get several windows at the same time.
- **Reviews** — `review_context(mr)` reads the MR, its CI, the discussions and
  the diff in parallel. It also gives the ticket key for the tool of the issue
  tracker. `comment_mr_lines` posts all the inline comments of a review in one
  call.
- **Logs and pods** — `query_logs(env, service, …)` and `k8s_status(env)` make
  the query from the environment name in `project.yaml`. They return compact
  lines in a code block. One ADC token gives access to Cloud Logging,
  Monitoring, the GKE API and the Kubernetes API of each cluster (the GCP pack,
  `packs/gcp`).

---

## meeseek and Claude Code

**What meeseek is.** meeseek is two things:

1. The context, and the way in and out:
   - Requests come in by Chat, Telegram, the Inbox triggers and the MCP
     server. Answers go back the same way.
   - meeseek keeps the knowledge of the team: rules, facts, notes, meetings
     and outputs.
   - meeseek makes the writes to the systems of the team, with your approval.
2. Plugins for agents. These are stateless calls that make an agent stronger.
   They look up the context of the team, draw, and get a review of a page
   (`mcp_server.plugin_tools`).

**Claude Code** (`claude:` in project.yaml, `runtime/claude_agent.py`) is an
agent that meeseek gives work to: code, debugging, reasoning, pages and
documents. It uses the Claude Code login of this machine (the subscription).
Its calls show on the Usage page as calls and tokens at US$0.

The handover goes one way, with no state between agents. meeseek sends a
message and a folder (any folder; empty = a scratch folder). Claude sends back
a reply and the files that it wrote.

Claude uses the meeseek plugins over the meeseek MCP server, and nothing else
from meeseek. It does not get `ask_meeseek`, MR or Jira writes, or the
connectors of the claude.ai account. Thus it writes to no team system. If a
task needs a write, Claude says what to write, and meeseek makes the write
with your approval.

Claude runs in the auto mode of Claude Code, inside its folder and the outputs
directory. A file write to any other location gets a refusal. Each action that
auto mode would stop to ask a person about also gets a refusal. Nobody waits
for an answer. meeseek does not load user or project Claude settings. The
CLAUDE.md / AGENTS.md of the folder goes into the system prompt of Claude.

**Sessions.** The Claude button next to the chat composer lists every Claude
session on this machine. `list_claude_sessions` also lists them. Claude
Desktop sessions have a mark and show their titles.

`search_meeseek_sessions` finds the past conversations of meeseek (console
chats, phone messages, Inbox turns, MCP requests) by what people said in them.
`get_meeseek_session` reads one back (`runtime/past_sessions.py`).

To use a Claude session, select one, or a new one. The tasks of the
conversation then go to it. Each message has a
`[Claude session: <id> · <title>]` line. The orchestrator reads this line and
gives the task to the session with `ask_claude(session_id=…)`. In Telegram:
`/claude` lists, `/claude 2` selects, and there are also `/claude new` and
`/claude off`. If a different program wrote to a session in the last ten
minutes, meeseek forks the session, so that two writers do not write to it.

**Pages.** Claude builds each web page, dashboard or visual report. Each one is
one self-contained HTML file under `outputs/pages/`. The conversation shows it
live. meeseek sends it to the phone as a picture of itself at phone width.

Claude cannot see what it built. Thus it calls the `review_page` plugin.
Gemini makes screenshots of the full page at phone width (500px) and desktop
width (1280px). Gemini then lists, by severity, the parts that are broken or
not readable. Claude fixes them.

Pictures have a boundary. Claude draws all programmatic pictures in code. For
an illustration, concept art or a designed visual, Claude asks Gemini with the
`generate_image` plugin.

meeseek records each session that it drives (`.cache/claude_tasks/`): every
message and reply, and every command that Claude ran.

Every page under `/outputs` (HTML or SVG, in the frame of the console or in a
tab of its own) has a CSP sandbox. The sandbox gives an opaque origin, no
network and no forms. Thus the script of the page cannot call the API of the
console.

---

## Surfaces

**Web console** (`/ui`):

- Chat. The Trace of each session (every event of a turn) is one toggle away.
- Inbox: the items that arrived on their own (see below).
- Usage: tokens and cost by agent and model.
- Context (see below).

The URL shows the location (`#/chat/<session>`, `#/inbox/<item>`, …).

**HTTP API**: the API of the console, and the API of meeseek:

- conversations under `/api/sessions` (list, create, read with their events,
  delete)
- a turn as a stream of its events, from `POST /api/sessions/{id}/turns`
- a turn that stopped, continued from `POST /api/sessions/{id}/resume`
- the files of a message, at `/api/files/{ref}`
- `/inbox_api`, `/context_api`, `/learning_api`, `/usage_api`,
  `/feedback_api`, `/auth_api`, `/claude_api`, `/slash-commands`

The event shapes are in `surfaces/console.py`. When the server runs, it
documents every route at `/docs` (OpenAPI: `/openapi.json`).

**MCP server** (`/mcp-server/mcp`), for Claude Code or Claude Desktop:

```json
{ "mcpServers": { "meeseek": { "type": "http", "url": "http://127.0.0.1:8787/mcp-server/mcp" } } }
```

| Tool | For |
|------|-----|
| `ask_meeseek(request, session_id, wait_seconds, image_paths, use_clipboard_image, caller)` | A request to the agents of meeseek. Returns `status` (`answered` / `needs_input` / `running` / `interrupted` / `failed`), `answer`, `handled_by`, `session_id`, and `task_id` if the request takes longer than the wait (with `wait_seconds: 0`, it returns the task immediately). A call with a `session_id` and no `request` continues a turn that stopped part-way, from its last step |
| `check_meeseek(task_id, wait_seconds, stop)` | Shows the status of a task (progress, then the result), or stops the task |
| **Plugins** — `kb_grep` / `kb_read` / `kb_list`, `fetch_url` (reads Google Docs/Sheets links with your login, through the Google pack), `search_meeseek_sessions` / `get_meeseek_session`; `generate_image` / `render_diagram`; `review_page`; and every read of each running pack, available when the pack runs — for example `jira_search` / `jira_get_issue`, `list_mrs` / `get_mr` / `search_code` / `review_context` / `get_job_log`, `search_gmail` / `list_chat_spaces`, `query_logs` / `list_alerts` / `k8s_status` | The plugins of meeseek (`mcp_server.plugin_tools`): every registry tool that changes nothing outside the outputs of this machine, and every pack read. They look up the context of the team, draw (Gemini images, Mermaid), and let Gemini examine a page that an agent built. Each one is one stateless call, with no agent turn behind it. A Claude session that meeseek gives work to can use these plugins, and nothing else from meeseek |
| `list_outputs` | Reports, reviews, pages and pictures (also `outputs://` resources) |
| `update_mr` / `create_mr` / `comment_mr` / `comment_mr_lines` / `reply_mr_comment` | The MR writes of the GitLab pack, with no routing turn (`mcp_server._DIRECT_WRITE_TOOLS`; available when the pack runs) |

meeseek makes the direct tools from the same registry that the agents use.
Thus the two surfaces cannot become different. Jira and Confluence writes go
through `ask_meeseek`, so the SOP of the skill that owns them applies. Images
go by reference (`image_paths` in `AGENT_IMAGE_ROOTS`, or the clipboard on
macOS). The reason: a host model cannot send image bytes again.

> Keep the server on a trusted network, or put auth in front of it. Its
> endpoints have no authentication, and the agents can write (MR comments,
> Jira tickets).

**Scheduled jobs** (`server.py`):

| Job | When |
|-----|------|
| Drive sync | every 30 minutes |
| The Inbox triggers | every minute (when a trigger is on) |
| The meeting recorder | every 15 seconds (when it is on) |
| Meet cloud recordings of meetings that the laptop did not record | every 5 minutes (when the calendar is on) |
| The wakes poll (below) | every 30 seconds |
| The Inbox digest | on `triggers.digest_cron` |
| The daily review | on `triggers.review_cron` (weekdays 18:30) |
| The weekly rule review | Mondays 02:00 |

To backfill recordings in the Drive meetings folder, use `/meetings` by hand.
meeseek skips the recordings that it already made into notes.

**Wakes and tasks** — the promise that something occurs later, on disk
(`runtime/wakes.py`, `runtime/tasks.py`).

A wake is one record: when to act, which event makes it due early, and why.
meeseek polls the wakes every 30 seconds. After a restart, meeseek claims them
again and does not forget them. These are wakes:

- the Inbox retries
- the deferrals for quiet hours
- the Claude Code follow-ups. If a task continues after the turn that asked
  for it, the task retargets its session when it ends.

A task is a goal that the person agreed to: a success condition, progress and
notes. A task continues across turns, and a `task_check` wake moves it
forward. Only the console can close a task.

The harness holds replies to this rule. If a reply says "I'll report back"
and nothing schedules the report, the harness sends the reply back for a
rewrite.

**What autonomy may cost** has caps in `project.yaml`:

- `triggers.daily_turns`: the number of autonomous turns in a day. After this
  number, items file as fyi, and you can run them by hand.
- `models.turn_minutes`: the wall-clock minutes for each turn that nobody
  watches. After this time, the tools answer "time is up, wrap up".
- `triggers.quiet_hours`: during these hours, the pushes of the bot wait as
  wakes.

`triggers.digest_cron` sends the phone a summary of the work that finished
unseen: answers, held writes and open questions. Below the summary is
全部標為已讀. If nothing changed, it sends nothing. A write that waits for
your yes for more than 3 days is in the next digest one time, even when you
saw it. One day after that digest, the write expires: it does not run, and
the item status is `expired`. With no digest (no `digest_cron`, Telegram or
`coach_telegram_chat`), the write expires after 3 days.

`triggers.review_cron` runs the daily review (`runtime/daily_review.py`). The
review reads the last 24 hours: the log warnings and errors, the failed or
retried turns, your corrections and dismissals, the failed Inbox items and the
sources that are down. It reads only, and it limits the size of what it reads.
Claude Code gets this bundle in meeseek's own folder, read only, and finds the
root causes. The result is one Inbox item, "Daily review — <date>", with the
fixes ranked by the manual time that each fix saves. Each fix is a held
action. When you approve a fix, a Claude session implements it in a new git
worktree, on a new branch, and commits. It does not push. The review counts as
one autonomous turn. If nothing is new since the last review, it files
nothing.

**Inbox** — work that starts on its own. meeseek polls the sources that are on
under `triggers.sources` in project.yaml (`runtime/triggers.py`). Each source
belongs to a pack:

- mail, direct messages and @mentions in Chat, and meetings that are about to
  start (the Google pack)
- open Cloud Monitoring incidents (the GCP pack)
- reviews that people ask of you, and comments on your MRs (the GitLab pack)
- tickets that people assign to you, comments that mention you, and comments
  of other people on your tickets (the Jira pack, `jira_poll`)

One conversation (a Chat space, a mail thread, an MR, a Jira ticket) is one
item. The next
message in the conversation updates the item. If that message needs work, it
gets a turn in the same session. When you write in the conversation yourself,
the item is done. On a Jira ticket, a comment or a status change by you
makes the item done. The brief or the notes of a meeting are done when you
open them. The brief of a meeting is also done when the meeting ends.

Some messages go to the item of a different conversation:

- A notification mail from Jira about a ticket, or from GitLab about an MR,
  goes into the item of that ticket or MR. It does not change the status of
  the item, and it does not start a turn. The Jira and GitLab sources tell
  meeseek what on the ticket or MR needs you. Before such an item exists, the
  mail makes it, as fyi.
- A Chat message that has a link to one MR goes into the item of that MR. The
  item then gets a turn that makes a first-round review as drafts.

meeseek settles bulk mail and invitations by their headers. The fast model
triages each mail or message. If an item needs you, it gets an orchestrator
turn in its own session (owner `inbox`). You can open this session in Chat and
continue it. An alert gets a first debug, and a meeting gets a brief. In these
sessions, the agent only reads and writes drafts (`runtime/inbox.py`):

- A reply is a draft. The Inbox page shows it. You copy it and send it
  yourself. The Workspace login stays read-only.
- meeseek holds every write, and lists it on the item as an action that waits
  for you. Approve it (you can edit the arguments first), and the next turn
  tells the agent. Exactly that call runs, and the agent continues to its next
  step. The writes of that step wait again. If you reject a write with a
  reason, the agent adapts.

The console polls the Inbox and shows a badge on its tab. It shows a desktop
notification when an item starts to wait for you.

**From your phone** (`messengers:` in project.yaml, `runtime/messengers/`):
send messages to the orchestrator from Telegram. A message is an Inbox item.
Thus it gets the same session and the same gate. The answer comes back to the
chat after every turn. Each write that the turn wants comes as an Approve /
Reject pair that you tap in the chat. The Inbox page also works, and meeseek
sends its turns to the phone.

Messages continue one conversation until `/new`, or until eight hours with no
messages. The bot answers only the user ids that you list. If you list none,
the bot replies with the id of the sender, so that you can add your id. The
answers go through the servers of Telegram.

The bot shows an answer in Telegram formatting: bold, code blocks, links,
quotes, and tables as monospace columns. The files that the turn made under
`outputs/` come after the answer: images as photos, and other files as
documents.

To set up the bot:

1. Make a bot with @BotFather.
2. Put `TELEGRAM_BOT_TOKEN` in `.env`.
3. Set `messengers.telegram: true`.

The bot uses long polling, so it exposes nothing.

**Meeting recording** (`triggers.record_meetings`, macOS): when you are in a
Google Meet in Chrome, `recorder/meeting-recorder` records the system audio
and your microphone, and nothing else (`runtime/meeting_recorder.py`). The
recorder uses ScreenCaptureKit, and meeseek builds it on first use.

It is sufficient that you are in the meeting. It does not matter who called
the meeting. meeseek finds a meeting in these ways:

- by its tab
- by its window title, matched to the calendar, when the window is full
  screen. A full-screen Chrome window is not in the tab list at all.
- by its title only, when it is on no calendar (you joined from a link that
  you pasted)
- by the calendar, when meeseek cannot read the Chrome tabs at all

While the calendar says that a meeting is on, a tab that is missing from the
list does not stop the recording. Minimised and full-screen windows go out of
the list while the call continues.

When the meeting ends, meeseek mixes and keeps the tracks. The meeting-notes
pipeline transcribes them into `knowledge_base/sources/meetings/`.

Then meeseek finds the action items in the notes that are yours or your
team's (`triggers.meeting_tickets`, `runtime/meeting_tickets.py`). For each
one, it searches Jira first. If a ticket has the work, it proposes a comment
on that ticket. If no ticket has the work and the action item is yours, it
proposes a new ticket, assigned to you. For an action item of your team, it
proposes a new ticket only with `create_for_team: true`. Each proposal is an
action on the item of the meeting, and waits for your yes. You approve or
reject each one. The agent turn of the item then lists the decisions and the
to-dos, and which ones have a proposal. With no `meeting_tickets.project`,
the agent turn searches Jira and proposes the tickets itself. macOS asks one time for access to
Screen & System Audio Recording, Microphone and Automation (Chrome), for the
app that the server runs in.

**Meeting coach** (`triggers.coach_meetings`, `runtime/meeting_coach.py`). The
coach helps what you say in meetings to have more effect. It works for the
meetings that it names (by their titles on the calendar).

When a meeting is about to start, it gets a rehearsal in place of a brief. The
rehearsal contains:

- what you will report from Jira and GitLab
- what people can ask you, and how to answer
- the one thing to push for
- a 60-second draft to speak
- what the last report told you to practise

After a meeting that meeseek recorded, the coach writes a report on how you
came across. The coach makes the report from a transcript of the two tracks.
The report contains:

- numbers that code measures: your talk time and share, turns, filler words
- delivery, content and influence
- the rehearsal compared with what you said
- rewrites of your own sentences
- what to practise next
- 向他人學習: the moments where the other people chaired, paced, gave
  direction, phrased or reasoned in ways that you can copy. For each moment:
  who, when, the line, and how to use it. `triggers.coach_learn_from` names
  the people to watch specially.

With a headset at home, the laptop microphone hears only you. On the speakers
of a meeting room, it hears everyone. Thus meeseek finds who spoke from the
voices and the context:

- a microphone that hears one voice
- the people that others address by name, and who answers
- what each person reports, compared with the roster and the team graph
- what the rehearsal says that you would report

Each ten-minute window continues from the names that the window before it
settled. The notes give a name from the context to each line that is still
與會者, with the mark （推斷）.

(meeseek read the live captions of Meet for this one time. In the room
meetings that the captions were for, the captions named the room, not the
speaker. The transcripts were the same without them.)

If a meeting continues after its calendar end, the recording continues while
its tab is still in the call. For this check, Chrome needs View → Developer →
Allow JavaScript from Apple Events.

The report, the transcript and the recording go into
`knowledge_base/outputs/meetings/<date-time-title>/`. The reply of the item
starts with them. If you set `coach_telegram_chat`, the rehearsal and the
report (with its files) also go to that one Telegram chat. Increase the
`lead_minutes` of the calendar source, so that the rehearsal comes early
enough for you to read it.

Sometimes the laptop does not record a meeting (your meeting, or a meeting
that the coach names). Then meeseek uses the Meet recording in its place
(`runtime/meet_cloud.py`). Do this one time: set the recurring meeting to
record automatically (Calendar → the event → video call settings → Meeting
records). meeseek then gets the recording that Meet attaches to the event,
read-only, and completes it like a local recording. In both cases, meeseek
transcribes a meeting one time, with who said what
(`runtime/meeting_transcript.py`). It makes the notes and the report from that
transcript.

---

## Context and learning

On the Context page of the console, you can create, read, update and delete
what the agents know:

- learned rules and facts, one entry at a time (through the same checks as a
  write by an agent)
- pinned notes, other notes and meeting notes, as files
  (`runtime/context_store.py`)

The files stay the store, and the agents read them directly. A save contains
the version that you opened. Thus a save never overwrites an edit that the
IDE, a sync or an agent made in the meantime. You cannot edit Drive sources
and `SKILL.md` there, because Drive and git own them.

Agent outputs are not context, because no agent reads them back. Thus you open
them from the conversation that made them. A path to an output in a tool
result or in a reply opens the full output, read-only.

Two things change what an agent does in the next turn. meeseek writes them
only after you say yes (`runtime/consent.py` holds the call until you do):

- **Rules.** When you correct an agent, it asks you whether to keep a rule. If
  you say yes, `propose_sop_rule` adds the rule to `skills/<name>/learned.md`.
  The rule is live from the next turn. A rule must pass a quality check. It
  must not repeat a rule that is already in the SOP or learned.md. It keeps
  the conversation that it came from as a replay case. A skill holds a
  maximum of 10 rules. At the cap, the agent shows you the list and asks which
  rule to remove.
- **Facts.** `remember_fact` adds facts to `notes/facts.md`. The newest 20
  facts go to every agent, each for 90 days. Then they leave the file. If a
  fact must stay, put it in `notes/pinned/`.

meeseek learns two more things without a question. Both follow one lifecycle
(`context/lifecycle.py`), so that neither one becomes too large:

- **The team graph** (`context/ontology.py`): who belongs where, who gives
  work to whom, and who owns what. The roster `notes/pinned/team_members.md`
  is the authority. The other names of a name, in brackets
  (`Wayne (崇恩、崇安)`), are the same person. Each meeting adds what it shows.
  If meetings showed a thing more than one time and the roster does not say
  it, that thing goes to every agent.
- **What holds of you** (`context/personal.py`): your preferences, what not
  to do, and how you work. meeseek reads your conversations in the console and
  from your phone each night. `notes/about_me.md` is the one file for this
  data.

  Your part at the top of the file is the authority. A line under a heading is
  confirmed, and a line under 「不成立」 is ruled out. meeseek rewrites its
  「推斷」 part at the bottom with what it inferred. Each item has its count and
  evidence. To confirm an item, move its line up. The things that you
  practise come from the last coach report, and only the coach uses them.

A learned item has one of these states:

| State | When | Retired |
|-------|------|---------|
| A guess | seen one time | after 30 days, unless seen again |
| A pattern | seen in two sources | after 180 days if it does not occur again |
| Confirmed | a person wrote it down | never; kept until the person removes it |

A rewording is the same item, seen again. Every save removes the items that
have expired. A nightly tidy (03:30) also removes them. Each section of the
per-turn memory has its budget.

**Learning from use** (at the harness level: the SOPs, prompts and rules
change, and the model does not change). meeseek records every turn with the
version of the policy that made it (`runtime/trajectory.py`,
`runtime/policy.py`). It also records the result of the turn
(`runtime/feedback.py`):

- your 👍/👎 on a reply (console and phone, with a note on what was wrong)
- an Inbox write that you approved, edited (meeseek keeps the diff) or
  rejected
- an item that you dismissed, and the reason
- your next message, if it corrects the reply or asks again
- the marks of the harness itself: a failed write that the reply skipped, or
  a tool budget that ran out

`runtime/reward.py` makes a reward from these records. What you did has a
higher rank than what meeseek inferred. A model judges only the turns that
nobody labelled, and only while it agrees with you. The Learning page shows
the scoreboard of each skill. Graded turns become a case library, and each
case keeps its conversation (`runtime/cases.py`).

Each Monday, inside the process of the rule review, the improvement run
(`runtime/improve.py`) does these steps:

1. It takes the group of turns with the worst results.
2. Two proposers suggest a maximum of three edits to the SOP or rules of that
   skill. An edit must not make larger what every turn carries, and must stay
   inside the SOPs and rules.
3. It replays the cases under each candidate, with stubs in place of writes.
4. It compares the replies blind, in both orders.
5. If a candidate did better and did not break a case that went well, the run
   proposes the candidate to you. The proposal shows the diff, the result for
   each case and the cost, on the Learning page and on your phone.

**Nothing changes until you approve**. When you approve, meeseek commits the
edit. The agents use the edit on their next turn, with no restart. If the
skill then does worse, meeseek proposes a rollback in the same way. The run
stays inside `learning.weekly_usd` (US$5) and `learning.max_replays`. Records
older than eight weeks fold into weekly lines. Conversations older than
`learning.session_days` (180) leave sessions.db.

Context costs money on every turn, so meeseek reviews the rules weekly
(`runtime/rule_review.py`). It replays the original request of each rule with
the rule and without it (with stubs in place of writes), and judges the
results. Sometimes two reviews in sequence find a rule redundant (the SOP
already does it) or not effective (the agent does not follow it, even with
the rule). Then meeseek removes and archives the rule, and you can restore it
from the Context page. If two reviews find that a rule works, meeseek offers to
graduate the rule into `SKILL.md` as a git commit. The summary at the top of
the rules on the Context page is the latest review.

---

## Configuration

- **`project.yaml`** — everything that is not secret: team, Drive folders,
  Jira projects, GitLab repos (the allowlist that every GitLab tool enforces),
  K8s environments, model tiers, MCP servers. It is gitignored. The template
  with annotations is [`project.yaml.example`](project.yaml.example).
- **`.env`** — secrets only. See [`.env.example`](.env.example).
- **Your context's home.** The knowledge base and all the data that meeseek
  keeps for you (`knowledge_base/`, `.cache/`) are under `MEESEEK_HOME`. The
  skills are under `MEESEEK_SKILLS`. Both have this checkout as the default.
  Set them to a folder of your own, and the context moves with you, separate
  from the code: to a different machine, a different coding agent, a different
  job.

meeseek uses no Google API keys. Every Google service authenticates through
ADC.

**Model tiers.** Skills name a tier (`router`, `workflow`, `fast`, `coding`;
also `multimodal` and `image`). You set the tiers one time under `models:`. A
tier can point to any [litellm](https://docs.litellm.ai/) id (`openai/…`,
`bedrock/…`), with the credentials of that provider in the environment.
Claude on Bedrock also needs `uv sync --extra bedrock`.

**Knowledge base.** `knowledge_base/notes/` is yours and gitignored.
`pinned/*.md` and `facts.md` go to every agent on each turn. `kb_grep`
searches the dated notes and `sources/` (synced from Drive). See
[`knowledge_base/notes/README.md`](knowledge_base/notes/README.md).

**Prompts.** Each engine-owned text that a model gets is a file under
[`src/meeseek/prompts/`](src/meeseek/prompts/README.md). Its README lists who
gets each file and where meeseek assembles it. Prompts and SOPs use
`{{placeholders}}` that meeseek fills from `project.yaml`: `{{team_name}}`,
`{{teams}}`, `{{jira_project_key}}`, `{{ticket_prefix}}`, `{{board_name}}`,
`{{jira_component}}`, `{{gitlab_project_path}}`, `{{k8s_environments}}`,
`{{k8s_env_names}}`, `{{default_gcp_project}}`.

---

## Extending

### Add a skill

Make the file `skills/<name>/SKILL.md`. The directory name is the name of the
skill.

```markdown
---
description: >
  One line on what this skill does.
  Keywords: words that should route here
model: workflow               # tier alias or a literal model id
tools: comment_mr, create_mr   # WRITE tools only — reads are automatic
mcp: false                    # true = bind the MCP servers that list this skill in project.yaml
ship: reports                 # optional: ship_output → outputs/reports/
limits:                       # optional: per-tool argument caps
  list_timeseries: {pageSize: 100}
search_limits:                # optional: per-turn call counts before "converge"
  kb_grep: 6
routes:                       # optional: what the router sends here beyond the description
  - "a ticket link; a ticket screenshot"
---
你是…（角色一句）

## 流程
1. ...

## 完成標準
What "done" means — claim only what tool results prove.
```

Restart, and meeseek finds the skill. These errors stop the startup: an
unknown key, a write tool that does not exist, or a read tool under `tools:`.

The router routes on the descriptions, which the `transfer_to_agent` tool
lists. It also routes on the `routes:` of each skill (the shape of a link, a
type of screenshot), which the prompt of the router lists.
`prompts/agents/router.md` names no skill.

Reply style, sources, questions to the user and tool failures come from
`common.md`. A SOP does not repeat them. A tool in the `tools.py` of a skill is
attached to that skill only.

### Add a tool

Write the tool under `src/meeseek/tools/`. Register a `_ToolDef` in
`tools/registry.py`. If the tool changes anything outside the process, mark it
`writes=True`. Then grant it to a skill and to `test_security_policy.py`. Put
the rules for its use in its description.

---

## Testing and evaluation

```bash
uv run --extra dev pytest                       # unit + mocked integration
uv run --extra dev pytest -m e2e                # live APIs
uv run --extra dev python -m evals.harness.run --models lite --runs 5   # harness eval
uv run python -m evals.turn_cost                # what real turns cost
uv run python -m evals.prompt_sizes             # static text per model call
```

The harness eval runs real agents against real reads, with stubs in place of
all writes. It checks behaviour:

- The agents report failed writes.
- The agents ask about vague requests.
- Logs come back raw.
- Reviews start from one context call.

See [`evals/README.md`](evals/README.md).

---

## Project structure

```
src/meeseek/
  core/          what a turn is — its events, what a tool is — in plain Python
  ports.py       the seams the core needs from the world, as Protocols
  adapters/      the only code that names a vendor or store: llm (Gemini, LiteLLM, an OpenAI-
                 compatible gateway), mcp, sqlite (events, the scheduler's lease), memory, blobs
  engine.py      meeseek's agents, tools and policies, wired into core's turn loop
  app.py         one meeseek: its config, home and shared runtime state; build_app()
  surfaces/      the web console's session and streaming routes
  config/        project.yaml + .env loading, the home your context lives in, ADC auth
  context/       placeholders, memory, Drive and meeting ingest, outputs
  prompts/       every engine-owned prompt, by name
  runtime/       skill loader, history, harness, Claude Code, MCP server (plugins), learning, inbox and triggers
  tools/         the registry and the tools: knowledge base, web, drawing, Claude Code, …
packs/           vendors as MCP servers, each its own package: kit (what a pack is made of), jira, gitlab, gcp, google, github (the example)
skills/          one folder per skill
recorder/        the meeting recorder (Swift, ScreenCaptureKit)
evals/           harness eval, prompt sizes, cost measures
frontend/        the web console (React), served at /ui
  src/           App.tsx and main.tsx; chat/, views/ (Inbox, Context, Usage, Trace, the sidebar),
                 markdown/, ui/, lib/ (API, routes, helpers), styles/ (in cascade order)
  tests/         unit tests (node --test) and ui_smoke.py
knowledge_base/  notes, synced sources, outputs (gitignored)
scripts/         setup.sh, doctor.sh, serve.sh
server.py        the web server and scheduled jobs
```

---

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md). The license is MIT. See
[LICENSE](LICENSE).
