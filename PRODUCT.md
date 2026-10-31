# PRODUCT.md

## What this is

meeseek is a personal AI teammate. It is always on, and you run it on your own machine.

It is a multi-agent server: a router and 4 skill agents (code, infra, research, sprint).
It keeps your working context:

- What you know.
- What you decided.
- What you wait for.

It operates the systems that you connect:

- An issue tracker and a wiki.
- A git host.
- Cloud observability.
- A synced knowledge base.
- Meeting transcription.

meeseek gives implementation and all other heavy work to a coding agent (Claude Code) on the same machine.
The context is yours and it is portable.
The context moves with you when you change coding agents, teams or jobs.

## Who uses it

One engineer (or a small team that shares a deployment) uses it each day, from a laptop.
These users know chat AIs, coding agents and dev consoles well.
They value speed, density and trust more than spectacle.

## The surface: Web Console (`frontend/`)

The Web Console is the one place where you use meeseek. It has four views:

1. **Chat** — In this view, you can:
   - Converse with the orchestrator (SSE streaming).
   - Manage sessions.
   - Answer the questions of the agents.
   - Read markdown, tables, diagrams and artifacts.

   Each session has a **Trace**, and one toggle shows it. The Trace shows which sub-agent ran, the tool calls with args and results, the timings and the errors.
2. **Inbox** — This view shows what arrived on its own (mail, chat, alerts, meetings). It also shows what the agent made of each item:
   - A reply draft that you can copy.
   - The writes that the agent wants. Each write waits for your yes.
3. **Usage** — This view shows tokens and cost by agent and by model.
4. **Context** — This view shows all that the agents know and that you can change:
   - Learned rules (with the weekly review and the archive).
   - Facts.
   - Pinned notes and other notes.
   - Meeting notes.

   Drive sources and SOPs are not in this view, because Drive and git own them. You open agent outputs from the conversation that made them.

Mode: **Operate** (the user is in a task; the tool should disappear).

## Brand commitments

- Name and mark (user-pinned, 2026-10-10): **meeseek**, the bracketed-check mark.
  The visual direction is tech and geek, and readable first.
  It is a terminal-flavoured ops console in light and dark, with Geist and Geist Mono and one signal colour.
- UI copy: **English**. The UI shows only what the screen needs (no host, port or ids in the chrome).
  Agent content shows as-is, in the language that the agent uses for its answer.
- No mascot and no borrowed brand: the mark and one signal colour carry the brand.

## Constraints

- The backend is the FastAPI server (`server.py`, port 8787). It has these routes:
  - The session routes `/api/sessions[/{id}]`, `POST /api/sessions/{id}/turns` and `/resume`.
    These routes use SSE, with one event for each step, result and reply (`surfaces/console.py`).
  - The console's own routes: `/inbox_api`, `/context_api`, `/learning_api`, `/usage_api`, `/auth_api`, `/slash-commands`.
- v1 has no auth layer. Bind the server to localhost or to a private network.
  The userId is a fixed "webui".
- The frontend must be a static SPA (Vite build). It must not have a server of its own.

## What success looks like

You do a full day of work in the console and you do not leave it one time.
In that day, you ask questions, you run /commands and you examine what the agents did.
You trust each component at first glance.
