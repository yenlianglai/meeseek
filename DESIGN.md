---
name: meeseek
description: Terminal-flavoured ops console, light and dark, for the team-agent web frontend (frontend/)
colors:
  chrome: "#f7f8fa"
  content: "#ffffff"
  inset: "#f1f3f6"
  hover: "#eaedf1"
  ink: "#14181d"
  ink-secondary: "#4a5360"
  ink-tertiary: "#5f6976"
  accent: "#1f6fb2"
  accent-hover: "#16548a"
  accent-soft: "#eaf3fb"
  accent-ink: "#ffffff"
  danger: "#b3261e"
  danger-soft: "#fbecec"
  ok: "#0e7a4c"
  ok-soft: "#e7f5ee"
  warn-soft: "#fff4e0"
  warn-ink: "#8a5a00"
  line: "#e2e5ea"
  line-strong: "#d0d5dc"
  dark-chrome: "#0a0c0f"
  dark-content: "#0d1014"
  dark-ink: "#e7eaee"
  dark-ink-secondary: "#a3adb8"
  dark-ink-tertiary: "#7d8896"
  dark-accent: "#6cc4ff"
  dark-line: "#1f262e"
typography:
  body:
    fontFamily: "Geist, ui-sans-serif, -apple-system, Segoe UI, PingFang TC, Noto Sans TC, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.55
  heading:
    fontFamily: "Geist, ui-sans-serif, -apple-system, Segoe UI, PingFang TC, Noto Sans TC, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    letterSpacing: "-0.01em"
    lineHeight: 1.3
  meta:
    fontFamily: "Geist, ui-sans-serif, -apple-system, Segoe UI, PingFang TC, Noto Sans TC, sans-serif"
    fontSize: "11px"
    fontWeight: 600
    letterSpacing: "0.02em"
  code:
    fontFamily: "ui-monospace, SF Mono, Menlo, Consolas, monospace"
    fontSize: "12.5px"
    fontWeight: 400
rounded:
  chip: "999px"
  control: "10px"
  card: "12px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "20px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    rounded: "9px"
    size: "32px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
  button-secondary:
    backgroundColor: "{colors.content}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "7px 10px"
  chip:
    backgroundColor: "{colors.chrome}"
    textColor: "{colors.ink-secondary}"
    rounded: "{rounded.chip}"
    padding: "1px 8px"
  card:
    backgroundColor: "{colors.content}"
    rounded: "{rounded.card}"
---

# meeseek — design system

Scope: this system governs `frontend/` (the React web console). The Python
backend has no UI surface. The direction contract is in `frontend/index.html`.

## Overview

The console is an Operate-mode surface. It is one calm, dense workbench. In
it, a teammate talks to the team agent. The teammate can always see what the
agent did. The register is the category standard (the user pinned it), at
the craft level of Linear and Notion. The tool disappears into the task. The
brand is in precise details, not in decoration.

## Colors

The console has a light theme and a dark theme. The system setting selects
the theme until the user selects one with the toggle in the top bar. The
browser keeps that selection.

Both themes use a restrained strategy: neutral colours plus one signal colour.
The signal colour is `#1f6fb2` on paper and `#6cc4ff` on the dark ground. Use
the signal colour only for these items, never for decoration:

- The primary action (send)
- The current selection
- Live state
- Links

Text contrast is 4.5:1 or more in both themes. Status colours differ in
lightness as well as in hue.

Every colour is a token on `:root`. The dark theme redefines each token under
`prefers-color-scheme` and `[data-theme="dark"]`. Do not hardcode a colour in a
component, because a hardcoded colour breaks one of the themes.

Mermaid diagrams keep a paper card in the dark theme, because mermaid draws
for a light background.

## Typography

The system uses two faces, and each face has one job:

| Face | Job |
|---|---|
| Geist (with PingFang/Noto TC for zh-TW content) | What people read |
| Geist Mono | What machines write: ids, paths, tool names, status, the wordmark |

That split is where the "geek" is. Long Chinese answers stay in the sans face
at 15px/1.7.

The pixel scale is fixed:

| Size | Use |
|---|---|
| 11px | Meta caps |
| 12.5px | Code |
| 14px | UI |
| 15px | Answers |
| 16–18px | Headings |

Timings and table numerals use `font-variant-numeric: tabular-nums`.

Every size has the form `calc(Npx * var(--fs))`. Below 1500px wide, `--fs` is
1. At 1500, 1800 and 2200px, `--fs` grows to 1.07, 1.15 and 1.25. The reading
columns scale with `--fs`. Thus a large screen shows larger text at the same
measure. Write a new font size in the same form.

## Header

One header spans every view. Nothing in the header moves when the view
changes. The header has these positions:

- The brand holds the width of the rail on the left.
- The list toggle keeps its slot. Where there is no list, the toggle is hidden.
- The view switch and the theme toggle hold the right edge.
- The items that belong to the view (the session name, Trace) are between.

A count on a tab floats on the corner of the tab. The count does not make the
tab wider.

## Brand

The name is `meeseek`. A meeseek is summoned for one task and is gone when
the task is done. The name is set in Geist Mono with a signal-coloured
underscore cursor (`meeseek_`).

The mark is one task in brackets, done. The brackets use the ink colour. The
check uses the signal colour. The mark is `Logo` in Icons.tsx and
`public/favicon.svg`. The favicon follows the system theme.

UI copy is English. It says only what the screen needs. Do not show a host,
port or session id in the chrome.

A running turn shows its state quietly beside the activity row
(`ui/Presence.tsx`):

- While the turn works, a soft accent pulse shows.
- When the turn is done, a check shows.
- When the turn failed, an alert shows.

The state appears after a short delay, so an instant turn does not flash.
Under `prefers-reduced-motion`, the pulse does not move.

The author line of the answer carries the mark, small, in the secondary ink.

## Layout

The layout is a 260px fixed session rail (chrome surface) and a fluid content
column. The reading column is 760px max. Trace tables extend to 980px. Below
860px, the rail becomes an off-canvas drawer under a scrim.

The spacing rhythm is 4/8/12/20/24. Groups sit tight. Separations are
generous.

## Navigation

The URL hash (`frontend/src/lib/route.ts`) holds the location in the console.
The console draws the screen from the hash. Thus back, forward, reload and a
pasted link all go to the same place. The rules below close every path:

- **One home per object.** Chat shows a session, and its Trace is one toggle
  away. Inbox shows an Inbox item. When the user picks an object anywhere, the
  console goes to the home of that object.
- **Every reference is a link.** An output that a reply names opens in place.
  A learned rule opens the conversation that it came from.
- **Layout follows scope.** Session pages (Chat, Trace) carry the session rail
  and the title. Team-wide pages (Inbox, Usage, Context) carry neither. Only
  one side list shows at a time.
- **A tab returns where it was left.** The selection on a page (item, file,
  range) is in the URL. Thus the user loses nothing when they switch away.
- **Focus.** The purpose of the page sits in a centered column at reading size
  (answers 15px). All other items fold:
  - ⌘B hides the side list. The console remembers this choice.
  - Steps fold above answers.
  - The session source is one select.

## Elevation & Depth

The system declares one elevation: 1px hairline borders (`line`) on the flat
plane. A shadow shows only on these items:

- True overlays: the mobile drawer, the slash palette and `shadow-pop` (offset
  and soft blur)
- Copy buttons that float over content
- The pressed thumb of the segmented control

Never use a border and a wide shadow on the same card.

## Shapes

| Radius | Use |
|---|---|
| 10px | Controls |
| 12px | Cards and the composer |
| 9px | Icon buttons |
| 999px | Pills, for chips only |

Hairlines are 1px `line`. `line-strong` marks interactive boundaries (the
composer, active borders).

## Components

- **Composer**: a white card with a `line-strong` border. On focus-within, the
  border changes to accent and a 3px `accent-soft` ring shows. The send button
  is the only filled-accent element on the screen. While a response streams, a
  bordered stop button replaces the send button.
- **Tool call chip** (`details`): a hairline card with a mono tool name and an
  args preview. The chevron turns 90° when the chip is open. In the running
  state, a 12px accent spinner shows. The result body is mono text on inset,
  with a max-height of 320px.
- **Segmented control**: a chrome track and a white pressed thumb with a 1px
  soft shadow.
- **Session item**: a ghost row. On hover, the background is `hover`. The
  current item has an `accent-soft` background and weight 500.
- **Agent chip (trace)**: a pill with a 6px colored dot. The dot colour comes
  from a themed 6-color lane palette.
- **Steps** (Chat): the tool calls of an agent fold to one quiet line above
  its answer. The line shows "N steps" and the tool names, or the tool that
  runs now with a spinner. The line counts failures in danger. The line opens
  to the tool rows. A message that only handed off folds into the next
  message, so one turn reads as one answer.
- **Slash palette**: a true overlay. It owns the `shadow-pop`. Selected rows
  use `accent-soft`.
- Every interactive component has default, hover, focus-visible and disabled
  states. For loading, use a skeleton shimmer (session list) or a spinner in
  the chip (tools). Never use a page spinner.

## Do's and Don'ts

- Do keep the accent scarce. Use one filled button per screen. Use the accent
  for selection and live state only.
- Do theme the browser surfaces: `::selection` (accent-soft), caret (accent),
  thin themed scrollbars and focus-visible rings.
- Do render agent markdown inside `.md` with hairline tables and inset code.
  Use the one `Md` component for this (GFM, LaTeX via KaTeX, highlighted
  code). A lone `$` is money, not math (`math.ts`).
- Do not add a second family or a display face. Do not use mono outside
  code, data and ids.
- Do not put motion on load. Use transitions only for state changes, and
  make each one 150–220ms long (pulse for live, shimmer for skeletons,
  rotation for spinners).
- Do not use a border and a shadow together for elevation. Do not use a
  radius larger than the pill radius on an item larger than a chip.
