# Goldfish review and drift report

Two ideas from the elephant-goldfish model, on top of workstreams
(`workstreams.md`). A goldfish reviewer remembers nothing: it sees only the
document and what the document references. A drift report compares the
documents with the code.

## Problem

- In six weeks, about 419 of the person's Claude Code prompts were corrections
  to a spec. The spec was not clear, not simple, or not ready to implement.
- The reviewer of a spec is usually its writer, in the same conversation. That
  reviewer knows what the spec leaves out, so it does not see the gaps.
- The design and engineering-management documents in the knowledge base drift
  from the main repo's `docs/` and code. Nobody compares them on a schedule.

## Goldfish review

`runtime/goldfish.py`. Three reviewers, each one model call
(`spec_review.model`, else `models.workflow`), in parallel:

| Lens | Question |
|---|---|
| `comprehension` | Can a reader restate the goal, the scope and the plan without help? |
| `zoom_out` | Is it as simple as possible? Does it build what it does not need, or build again what exists? |
| `readiness` | Can an engineer start now: paths, signatures, data, errors, tests, done criteria? |

Each reviewer gets the document and the references that the document names,
and nothing else. A reference is a link (read with `fetch_url`), a file in the
brief's folder or in the knowledge base, or a main-repo file such as
`docs/<name>` (read with the gitlab pack's `get_file`). The limits are 8
references, 6,000 characters each and 30,000 together. A reference that cannot
be read is listed as unread in the prompt and in the result.

Each reviewer returns findings `{severity: blocker|major|nit, where, what,
suggestion}`. The three lists are merged: two findings about the same place
with almost the same words become one, at the higher severity.

### In ask_claude

A brief of `spec_review.brief_chars` characters or more is a spec. The
workstream context does not count toward the length, but the reviewers read it
with the brief.

1. The first review finds blockers: nothing goes to Claude. The agent gets the
   findings and fixes the brief.
2. The agent calls `ask_claude` again (same conversation and folder, within
   three hours). This is the one re-review. The brief then goes to Claude, with
   all remaining findings in a section "Accepted trade-offs (spec review)".
3. A first review without blockers lets the brief go at once, with its findings
   as accepted trade-offs.

Each review is an event on the brief's workstream. The agent's report starts
with one line that gives the counts.

### In the research skill

The `review_spec` tool reviews a PRD or a design before `ship_output`. It
allows one review and one re-review for each document in a conversation. The
remaining findings go into the document as "Accepted trade-offs". The tool is
not a plugin: an MCP caller cannot spend its model calls.

### When the review cannot run

When no reviewer answers, the result says that the review did not run, and
why. In `ask_claude` the brief goes unchanged and the report says that it was
not reviewed. When one or two reviewers fail, the result names the lenses that
did not run.

## Drift report

`runtime/drift_report.py`, on `triggers.drift_cron` (Monday 09:00 by default).
It uses the same pattern as the daily review: gather a bounded bundle, start
one read-only Claude Code session, put the result in the Inbox.

1. Gather: the knowledge-base documents (`triggers.drift_docs`, else all Drive
   documents except meeting notes), the newest 12, each with its path, date and
   headings; the open workstreams; the main repo (the gitlab pack's first
   project).
2. Count one autonomous turn (`triggers.daily_turns`). Skip when the cap is
   reached, when Claude Code is off, or when there are no documents.
3. Claude Code works in the knowledge-base folder, read only. It reads the
   documents and checks their claims with the plugins `get_file` and
   `search_code`, at most 40 calls. It ends with a JSON list of
   inconsistencies.
4. One Inbox item, "Drift report — <date>", lists each inconsistency: what the
   document says, what the code or `docs/` says, and where. When there is no
   inconsistency, nothing is filed. When the comparison does not finish, the
   item is filed as failed and says so.
5. An inconsistency that names an open workstream's ticket or MR is also an
   event on that workstream.

## Configuration

```yaml
triggers:
  drift_cron: "0 9 * * 1"   # "" = off
  drift_docs: []            # parts of KB paths, e.g. ["engineering-plan", "api-spec"]

spec_review:
  brief_chars: 2500         # 0 = briefs are never reviewed
  model: ""                 # empty = models.workflow
```

## Cost

- One review is three model calls. With the default `models.workflow`
  (gemini-2.5-pro), each call reads up to about 20,000 tokens and writes about
  2,000 to 4,000 (thinking included): about USD 0.03 to 0.06 a call, so about
  USD 0.10 to 0.20 a review. A brief that has a re-review costs two reviews.
  With gemini-2.5-flash, a review costs about USD 0.02 to 0.04.
- The drift report is one Claude Code session a week, on the Claude Code
  subscription (no API cost), and one autonomous turn of the day's budget.

## Limits

- The re-review is matched by conversation and folder, not by the brief's
  text. A different spec sent in the same conversation within three hours, after
  a send-back, gets the re-review instead of a first review.
- The drift report does not track inconsistencies from week to week. The same
  inconsistency is listed each week until one side changes.
