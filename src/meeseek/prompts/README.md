# prompts/

Every engine-owned piece of text a model receives, one Markdown file per prompt
or per module. Code reads them by name:

```python
from meeseek.context import prompts

prompts.text("agents/router", slash_tool_commands=...)    # a whole file
prompts.text("runtime/callbacks#grace", budget=40)        # one section of a file
```

`grep -rn 'runtime/callbacks#grace' src` finds every place a text is used;
`tests/unit/test_prompts.py` fails on a file or section nothing reads, a name
that does not exist, or a call that leaves a placeholder unfilled.

## Reading a file

- **Whole files** (`agents/`, most of `blocks/` and `internal/`) are sent byte for
  byte. A trailing newline in the file reaches the model.
- **Sectioned files** (all of `runtime/`, plus `blocks/memory.md`, `blocks/tasks.md`,
  `agents/claude_agent.md`, `internal/page_review.md`, `internal/goldfish.md`) hold several short
  texts. A section starts at a line `## name` (lowercase ASCII, alone on the
  line) and runs to the next one; the blank lines around it are not part of the
  text. Anything before the first section is a note for the reader. A heading in
  any other form (`## 回覆風格`, `## Coding Conventions`) is prompt content.
- The blank lines that join a text to what comes before it (`"\n\n" + ...`) are
  added in code, at the call site the table names.
- `{{name}}` placeholders are filled from project.yaml (`team_name`, `teams`,
  `k8s_env_names`, … — `context/prompts.py:_get_placeholder_values`; a pack
  adds its own, such as the Jira pack's `jira_project_key`) or by
  the caller. A placeholder neither fills raises. Values are inserted as they
  are: braces inside a value are never rendered.

## How an agent's instruction is put together

An agent receives three kinds of static text, each with one home:

1. **Instruction** = its role (`agents/router.md`, or the skill's `SKILL.md` body)
   + `blocks/common.md` + the per-turn memory block. The list of agents it may
   transfer to, with their descriptions, is the `transfer_to_agent` tool's
   description (`core/turn.py`).
2. **Tool descriptions** — what a tool does and when to use it, including the
   rules that only matter to an agent holding it (when to draw, how to ask,
   consent before writing a rule or fact). They reach exactly the agents that
   hold the tool.
3. **Runtime messages** (`runtime/`) — what the runtime says about the state of
   the current turn.

## Manifest

Who receives it: **router** = the orchestrator; **every skill** = each
`skills/<name>/` agent; **headless** = an agent turn with no person on the other
end (the daily plan); **internal call**
= a single model call that is not an agent turn.

| File | What it is | Who receives it | Where it is assembled |
|---|---|---|---|
| `agents/router.md` | The orchestrator's static instruction: the focus gate (step 0), routing and disambiguation, slash commands | router | `runtime/orchestrator.py:instruction` |
| `agents/claude_agent.md` | Appended to Claude Code's own system prompt when meeseek hands it work: its folder and meeseek's plugins (and that it writes to no team system), its discipline, pages and the picture boundary, its final message (`#system`); the folder's CLAUDE.md / AGENTS.md (`#notes`); what meeseek knows about the person (`#me`, from `context/personal.py`) | Claude Code, as meeseek's subagent | `runtime/claude_agent.py:_options`, `_notes` |
| `blocks/common.md` | What every agent is told after its own role: reply style, sources, when and how to ask the user, tool failures, attached images | router, every skill | `runtime/skills.py:common` (router: `runtime/orchestrator.py:instruction`; skills: `engine.py:skill_agent`) |
| `blocks/memory.md` | Headings of the per-turn memory block (team notes, what is known about the user, what meetings showed of the team, facts, learned rules) | router, every skill — each turn | `context/memory.py:memory_instruction` |
| `blocks/tasks.md` | The per-turn task block: the session's (or user's) open Tasks — goal, success condition, progress, recent notes — and how to write back to them | router, every skill — each turn with an open task | `runtime/tasks.py:instruction` |
| `runtime/callbacks.md` | Turn-limit messages: per-tool soft and hard caps, the tool-budget and time grace messages, the nudge after an empty final reply, the model-error reply, history compaction (summariser prompt and summary head), the truncated-result stub, the stub an older tool result is shown as after the model refused a request as too long | any agent mid-turn; `compaction_*` prompt goes to an internal call | `engine.py`: `say` (the policies' and the turn runner's texts), `summarize`, `compactor`; `runtime/results.py:cap_result`; `runtime/inbox.py` (model_error) |
| `runtime/llm.md` | "Split the call" when a tool call was cut off by the output limit; the no-access and invalid-login notices a person reads when a model call or turn fails on the Google login or IAM | any agent (split); the person (as the turn's reply or error) | `engine.py:say`, for `core/turn.py:TurnRunner`; `runtime/llm.py:failure_message` |
| `runtime/harness.md` | Failed-write and unkept-promise reconciliation, and a write an interruption left outcome-unknown (`resumed_write`): the `reconcile` and `promise` send-backs, failure lines, the appended "系統紀錄" notes | any agent | `engine.py:say`, for `core/policies.py`: `FailedWriteCheck`, `PromiseCheck`, `ResumedWrites`; `core/turn.py:TurnRunner.resume` |
| `runtime/wakes.md` | The messages a wake's turn starts from: a Claude Code task ended after nobody was left waiting — take the result over and report; a Task's check came due — push it forward | router, on the session that asked / the Task's session | `runtime/wakes.py:_claude_followup`, `_task_check` |
| `runtime/tasks.md` | What the task tools answer when the turn may not do that: opening or settling a Task is the console user's call | any agent holding the task tools | `runtime/tasks.py:task_create`, `task_update` |
| `runtime/replay.md` | The tool-error result a replayed write gets | agents in a replay (runtime/replays.py: harness eval, weekly rule review) | `runtime/replays.py:payload` |
| `runtime/learned_rules.md` | Why `propose_sop_rule` refused (quality, duplicate, at capacity) and what to do instead | every skill | `runtime/learned_rules.py:write_learned_rule` |
| `runtime/consent.md` | What a rule or fact write gets back before the user agreed: ask first | every agent holding `propose_sop_rule` / `remember_fact` | `engine.py:say`, for `core/policies.py:Consent` |
| `runtime/inbox.md` | What an Inbox session's write gets back while it waits for approval; the messages that tell the agent it was approved (with the arguments to use) or rejected | any agent in an Inbox session | `engine.py:say` (`InboxGate`); `runtime/inbox.py`: `approve`, `reject` |
| `runtime/trigger.md` | The request an Inbox item's first turn starts from, per source (mail, chat, MR, Jira ticket, alert, meeting about to start — a brief, or with `coach_meetings` a rehearsal; meeting just recorded — with or without the coach's report, and with the Jira proposals already held or not; a message from your phone), with the shared rules: read and draft only, replies in a ```reply block | router, in an Inbox session | `runtime/triggers.py:message_for`, `runtime/meeting_recorder.py:_finish`, `runtime/messengers/__init__.py:on_text` |
| `runtime/mcp_server.md` | Server-level instructions for an MCP host's model: what to delegate, `needs_input`, outputs | the external MCP host (Claude Code …) | `runtime/mcp_server.py:build_mcp_server` |
| `runtime/daily_review.md` | The daily review's brief: the day's errors, failed turns and corrections as data — find root causes, rank fixes by manual time saved, change nothing, end with a json list (`#review`); the brief of one approved fix, in its own worktree and branch, commit and never push (`#fix`) | Claude Code, as meeseek's subagent | `runtime/daily_review.py:run`, `carry_out` |
| `runtime/drift_report.md` | The weekly drift report's brief: the knowledge base's design and engineering-management documents (listed as data) against the main repo's `docs/` and code through the plugins, bounded, read only — each inconsistency (what the document says, what the code says, where) in a json list (`#report`) | Claude Code, as meeseek's subagent | `runtime/drift_report.py:run` |
| `runtime/daily_plan.md` | The plan's user message and the unread-source marker | headless: plan_my_day | `runtime/daily_plan.py:plan_my_day`, `_collect` |
| `internal/daily_plan.md` | System prompt of the morning plan | headless: plan_my_day | `runtime/daily_plan.py:plan_my_day` |
| `internal/page_review.md` | Gemini's visual check of a page an agent built, from phone and desktop screenshots, as JSON issues by severity (`#review`) — the `review_page` plugin | internal call (models.multimodal) | `runtime/page_review.py:review` |
| `internal/goldfish.md` | The goldfish review of a spec: three reviewers that see only the document and what it references — comprehension (`#comprehension`), zoom-out: simplicity, YAGNI, reuse (`#zoom_out`), implementation readiness (`#readiness`) — each a JSON list of findings by severity | internal call (`spec_review.model`, else models.workflow), before a long ask_claude brief goes and from the `review_spec` tool | `runtime/goldfish.py:_prompt` |
| `internal/meeting_coach.md` | The meeting coach's report on how the user came across — delivery, content, influence, against the rehearsal, rewrites, what to practise (`#report`) | internal call (models.multimodal) | `runtime/meeting_coach.py:report` |
| `internal/workstream_update.md` | Fold one exchange about a piece of work into its shared notes: goal, state, next, decisions, open questions, title | internal call (models.fast), after a turn on a workstream | `runtime/workstreams.py:update` |
| `internal/workstream_event.md` | Tell the workstream's Claude Code session about an outside event (a merge, review comments), to carry on with what it unblocks | Claude Code brief, opt-in (workstreams.route_to_claude) | `runtime/workstreams.py:_route` |
| `internal/triage.md` | Does a mail or chat message need you: reply / investigate / fyi / ignore | internal call (models.fast) | `runtime/triggers.py:triage` |
| `internal/rule_judge.md` | Does a reply follow a learned rule | internal call (models.fast) | `runtime/rule_review.py:judge` |
| `internal/meeting_transcript.md` | One transcript of a recorded meeting, ten minutes a call: the laptop's two tracks (or Meet's own recording), speakers named from voice and context, carried window to window, the user's lines marked 我 | internal call (models.multimodal) | `runtime/meeting_transcript.py:transcript` |
| `internal/proposer.md` | A candidate edit to a skill's policy from the turns it got wrong: `inv` (procedure, tools) or `dom` (domain knowledge, a rule), with the shared `rules` (≤3 edits, cost class, allowed targets, keep_base as normal, evidence as data) | internal call (models.workflow), weekly | `runtime/improve.py:propose` |
| `internal/pair_judge.md` | Which of two replies to the same turn is better, blind (A/B in both orders), by the person's correction when there is one | internal call (models.fast), weekly | `runtime/improve.py:pair` |
| `internal/reflector.md` | At most one lesson {cue, lesson, evidence, confidence, per-edit verdicts} from a week's probe, or no_entry; never a score | internal call (models.workflow), weekly | `runtime/improve.py:reflect` |
| `internal/turn_judge.md` | A blind score (1–5) and problem tags for a turn nobody labelled, by the skill's rubric (`skills/<name>/rubric.md`, else the default section); the turn as data, not instructions | internal call (models.fast) | `runtime/reward.py:judge` |
| `internal/personal_learn.md` | What holds of the user, from his conversations (console and phone): preferences, things not to do, habits, strengths, goals, what he follows — his own words for each, known facts in their known words, an empty answer as normal, pasted text as data not instructions, nothing about anyone else | internal call (models.workflow) | `context/personal.py:learn_from_chat` |
| `internal/ontology_learn.md` | Who is who, from one meeting: triples in the team graph's schema (classes and relations from `context/ontology.py`), each with the sentence it rests on, names mapped through the roster's aliases, known edges in their known words, an empty answer as normal, the transcript as data not instructions; code keeps only those that fit the schema | internal call (models.workflow) | `context/ontology.py:learn` |
| `internal/meeting_extract.md` | Turn a transcript into structured meeting notes (the whole message), putting the transcript's 與會者 lines to names from context, marked （推斷） | internal call (models.workflow) | `context/meeting_notes.py:notes_from_transcript` |
| `internal/meeting_actions.md` | List a meeting's action items from its notes, and whose each is: yours, your team's or another's | internal call (models.fast) | `runtime/meeting_tickets.py:propose` |
| `internal/meeting_tickets.md` | For each of your and your team's action items: a comment on the Jira ticket that has it, a new ticket, or nothing | internal call (models.fast) | `runtime/meeting_tickets.py:propose` |

## Not here

- **Skill SOPs** — `skills/<name>/SKILL.md` at the repo root. They are the
  operator's, not the engine's: rendered with the same placeholders and loaded by
  `runtime/skills.py`.
- **Tool descriptions** — next to their schemas: `tools/registry.py` and the
  docstrings of tool functions (`ask_user`, `propose_sop_rule`, `remember_fact`,
  `reconcile`, …). The schema and its description change together, and the
  rules about using a tool live there (see above).
- **Tool results** — the facts a tool returns (`Shipped -> …`, an API error, a
  verdict word such as `REVIEW PASSED`) stay in the tool's code. A sentence that
  tells the model what to do next is a prompt and lives here.
- **Generated tables** — the slash-command tables are built from the dispatch
  dicts (`runtime/slash_commands.py:slash_command_tables`). The list of skills
  the router can transfer to is built from each SKILL.md `description`
  (`core/turn.py:_transfer_spec`).
- **Memory** — team notes, facts and learned rules are runtime data under
  `knowledge_base/notes/` and `skills/<name>/learned.md`; only their headings are
  here (`blocks/memory.md`).
