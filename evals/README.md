# Evaluations

Two measures, each answering a different question:

- **Harness eval** (`evals/harness/`) — does the app behave right? Real agents,
  real reads, stubbed writes, graded per case. Run it before and after any
  change to prompts, tool descriptions or the turn loop.
- **Cost measures** — what a change costs: static prompt size per model call,
  and model calls, tool calls and tool errors of real turns.

Run them before and after a change to a prompt, a tool description or the
turn loop; say what moved in the commit message.

## Harness eval (`evals/harness/`)

The behaviour eval. Each case sends one or more user lines to the real app —
all agents, policies and reads — with every write stubbed (`runtime/replays.py`,
payload shapes taken from real sessions), and grades the calls and the final
reply against the case's expectations. The expectation keys are listed at the
top of `cases.yaml`.

```bash
uv run --extra dev python -m evals.harness.run --models default --runs 5
uv run --extra dev python -m evals.harness.run --models lite --runs 5      # every tier → gemini-3.1-flash-lite
uv run --extra dev python -m evals.harness.run --models lite --no-harness  # without the app's policies
uv run --extra dev python -m evals.harness.run --rescore --label lite      # re-grade saved replies
uv run --extra dev python -m evals.harness.run --only <case> --disable delegate_task   # A/B without a tool
uv run --extra dev python -m evals.harness.run --diff before after                  # two labels, case by case
```

Results land in `evals/harness/results/` (gitignored), and each run is indexed
with the policy version it ran (`runtime/cases.py` `record_run`), which is what
`--diff` compares. What the cases hold:

| Area | Cases | Passes when |
|---|---|---|
| Writes are reported as they happened | `create_ok`, `create_then_epic_link_fails`, `one_of_two_comments_fails`, `update_field_rejected`, `sop_rule_rejected`, `all_writes_ok`, `read_only_no_claims`, `next_turn_remembers_the_failure` | a failed or partial write is named in the reply; nothing is claimed that no write did, this turn or the next |
| Turns resume | `resume_after_dropped_connection`, `resume_after_pause`, `pause_while_a_write_is_in_flight` | an interrupted turn, resumed (TurnRunner.resume), finishes without repeating a read, and a write it left outcome-unknown is re-sent only after a read |
| Asking | `vague_request_is_asked_then_done`, `research_asks_before_deciding`, `correction_is_asked_before_recorded` | a decision belongs to the user → `ask_user`, and the answer resumes the same turn; a complete request is not asked about |
| Routing | `mr_link_goes_to_code`, `logs_come_raw_first` | the request reaches the owning agent |
| Work done by code | `logs_query_is_built_not_written`, `logs_come_raw_first`, `review_starts_from_one_context_call`, `inline_comments_go_in_one_call` | logs come from `query_logs`/`k8s_status` and reach the reply raw; a review starts from `review_context`; inline comments go out in one call |
| Delegation | `single_lookup_is_not_delegated` | a single lookup is not split into sub-agents |

Baseline (2026-10-09, the 19 cases then — one has been added since — × 5 runs): 95/95 on gemini-3.8-flash, 90/95 on
gemini-3.1-flash-lite. flash-lite's misses are `correction_is_asked_before_recorded`
(about half its runs: it calls `propose_sop_rule` and asks afterwards), plus
single runs elsewhere.

A change to a prompt, a tool description or the turn loop is run on both
models before and after; say what moved in the commit.

## Cost measures

Two scripts measure what a change costs, before and after:

```bash
uv run python -m evals.prompt_sizes                    # static text each agent sends per model call
uv run python -m evals.turn_cost --since 2026-10-09    # model calls, tool calls and tool errors of real turns
uv run python -m evals.turn_cost --caller mcp --json   # one caller group, for diffing two periods
```

`turn_cost` reads `.cache/sessions.db`. A tool's `repeats` counts turns that
called it three or more times — usually a parameter the model had to guess,
which is a candidate for code to build instead.
