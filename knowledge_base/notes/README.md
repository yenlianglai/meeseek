# Knowledge base — notes

This directory is **your team's knowledge**. It is gitignored (except this
README and `examples/`) so your content never leaves your machine.

## Layout

```
notes/
├── README.md              # this file (tracked)
├── examples/              # anonymized seed you can copy from (tracked, not searched)
├── pinned/                # hand-written notes every agent reads each turn
│   ├── team_members.md    #   who's who — also used for meeting speaker attribution
│   └── repos.md           #   repos/services the team owns
├── facts.md               # consented facts every agent reads each turn (90-day window)
└── YYYY-MM-DD_<topic>.md  # refinements written by the research skill — searchable
```

## How notes are used

- **`pinned/*.md`** and **`facts.md`** are memory: every agent (router and skills)
  receives them before each turn. Keep them small and high-signal — team roster,
  key repos, project goals, standing conventions. They are not returned by
  `kb_grep`, since every agent already has them.
- **`facts.md`** is written only after you confirm (`remember_fact`). Each line
  carries its date and source, and reaches agents for 90 days after that date.
- **`YYYY-MM-DD_<topic>.md`** are written by the research skill's
  `save_refinement`. Together with `knowledge_base/sources/` (Drive sync, meeting
  notes) they are the corpus `kb_grep` / `kb_read` search.

## Getting started

Copy `examples/repos.md` into `pinned/` and edit it for your team, and write
`pinned/team_members.md`, the team roster (a line per person, `- Name — role`;
meeting notes and the team graph read it) — or just start chatting: the
assistant will help you build the KB as you work.
