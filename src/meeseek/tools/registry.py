"""Tool registry — one definition per tool, one policy axis.

Public API:
    read_tools(media=…)           — every tool that changes nothing outside this
                                    process; attached to every agent
    tool_specs(*names)            — (name, description, fn, params) for consumers
                                    that build their own wrappers (the MCP server)
    tool_names()                  — the catalogue, for tests

The one policy fact a tool declares is `writes`: does calling it change
something outside this process? `writes=False` goes to every agent;
`writes=True` must be named in a SKILL.md `tools:` line and authorised in
tests/unit/test_security_policy.py.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Callable

from meeseek.tools.dci import kb_grep, kb_list, kb_read
from meeseek.runtime.page_review import review_page
from meeseek.tools.claude import ask_claude, claude_check, claude_sessions
from meeseek.tools.image_gen import generate_image
from meeseek.tools.mermaid import render_mermaid
from meeseek.tools.web import fetch_url
from meeseek.runtime.past_sessions import read as get_meeseek_session
from meeseek.runtime.past_sessions import search as search_meeseek_sessions
from meeseek.runtime.tasks import task_create, task_note, task_update
from meeseek.runtime.wakes import remind_me
from meeseek.runtime.workstreams import workstream_get, workstream_note
from meeseek.runtime.goldfish import review_spec


# ---------------------------------------------------------------------------
# Tool definitions — every consumer derives its view from this list.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _ToolDef:
    """Internal tool definition — single source of truth."""
    name: str
    description: str
    fn: Callable[..., str]
    parameters: dict
    # Does calling this change something outside this process?
    #   writes=False  every agent gets it, whatever its SKILL.md says.
    #   writes=True   per-skill authorisation, enforced by the policy test's
    #                 SKILL_EXTERNAL_WRITE_GRANTS; an unbound tool cannot be
    #                 called at all.
    writes: bool = False
    # When True the engine passes the images the user attached to the current
    # turn as `_attached_images` [(mime, bytes)] — so a tool can use them as
    # references.
    wants_user_images: bool = False
    # When True the wrapper passes `_origin` — the user and session of the turn
    # that called the tool — so a run it starts can link back to it.
    wants_origin: bool = False
    # When True the result is raw evidence in a code block (log lines, a pod
    # table) that the reply must show; runtime/evidence.py puts it there if the
    # model did not.
    shows_raw: bool = False


def _params(properties: dict, required: list[str] | None = None) -> dict:
    """Helper to build a parameters schema."""
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


_ALL_TOOLS: list[_ToolDef] = [
    # --- Knowledge Base (DCI — direct corpus interaction, no index) ---
    _ToolDef(
        name="kb_grep",
        description="Search the local knowledge base (PRD, API design docs, meeting notes) by regex/keyword, like grep -n -i -C. Returns file:line matches with context. Iterate: search one exact term, read the hits, refine.",
        fn=lambda pattern, also_matching="", path_filter="": kb_grep(pattern, also_matching, path_filter),
        parameters=_params({
            "pattern": {"type": "string", "description": "Regex or literal keyword (case-insensitive), e.g. 'rate.?limit' or '角色'"},
            "also_matching": {"type": "string", "description": "Comma-separated extra patterns the file must also contain (chained grep)", "default": ""},
            "path_filter": {"type": "string", "description": "Substring to restrict searched file paths (e.g. 'meetings')", "default": ""},
        }, ["pattern"]),
    ),
    _ToolDef(
        name="kb_list",
        description="List knowledge base files with size and last-modified date, grouped by directory. Use to explore corpus structure or find recent docs.",
        fn=lambda subdir="": kb_list(subdir),
        parameters=_params({
            "subdir": {"type": "string", "description": "Optional path substring filter (e.g. 'drive', 'refinements')", "default": ""},
        }),
    ),
    _ToolDef(
        name="kb_read",
        description="Read a window of a knowledge base file with line numbers (like sed -n). Use after kb_grep to inspect local context around matches.",
        fn=lambda path, start_line=1, num_lines=150: kb_read(path, start_line, num_lines),
        parameters=_params({
            "path": {"type": "string", "description": "Path relative to knowledge_base/ (as shown by kb_grep/kb_list) or bare filename"},
            "start_line": {"type": "integer", "description": "1-based start line", "default": 1},
            "num_lines": {"type": "integer", "description": "Lines to return", "default": 150},
        }, ["path"]),
    ),
    # --- Diagram ---
    _ToolDef(
        name="render_diagram",
        description=(
            "Render a Mermaid diagram to a PNG — for STRUCTURE: architecture, flows, sequences, "
            "state machines, dependencies (what something looks like → generate_image). Draw it "
            "without asking when the user asks how something works or connects. Copy the "
            "`![Diagram](...)` line from the result into your reply unchanged: it is the only way "
            "the user sees the picture. A syntax error comes back as the result — fix the Mermaid "
            "(start simple, then add detail) and call again. Renders locally; nothing leaves the "
            "machine. The last line of the result is the file's absolute path."
        ),
        fn=lambda mermaid, theme="neutral": render_mermaid(mermaid, theme),
        parameters=_params({
            "mermaid": {"type": "string", "description": "Mermaid diagram syntax (e.g. 'graph TD; A-->B; B-->C;')"},
            "theme": {"type": "string", "description": "Theme: neutral (default, professional), forest, dark, default"},
        }, ["mermaid"]),
    ),
    # --- Claude Code: meeseek's stronger subagent (runtime/claude_agent.py) ---
    _ToolDef(
        name="ask_claude",
        description=(
            "Hand work to Claude Code — a stronger model than you, on the user's subscription (no extra "
            "cost) — and get its answer. USE it for what a stronger model does better: writing or changing "
            "code, debugging, a repo-wide analysis, a long document or design, a web page or visual report, "
            "hard reasoning; and to continue a Claude session the user names (find it with "
            "list_claude_sessions). DON'T for a lookup you can do with your own tools. The hand-over is one "
            "way and complete: the prompt says the goal, the constraints, what to report — and the context it "
            "needs that you already have. Claude can look up more itself (knowledge base, Jira, GitLab, logs) "
            "and draw (Gemini images), but it writes to no team system: if the task needs a ticket, an MR "
            "comment or a message, it says what to write and you make the change. For a code change to be "
            "reviewed, tell it to commit to a new branch and not push. `cwd` is the folder to work in — the "
            "local repo or directory the task is about (empty = a scratch folder). A page it builds is one "
            "HTML file under outputs/pages/, shown in the conversation. It waits up to `wait_seconds` "
            "(default 240, max 900); a longer job keeps going, and its answer reaches the user where they "
            "asked. Relay its answer; do not redo its work. A long brief (a spec) is reviewed first by "
            "zero-context reviewers: if it comes back with blockers, nothing went to Claude — fix them and "
            "call again with the whole brief; the second call goes."
        ),
        fn=lambda prompt, cwd="", session_id="", wait_seconds=240, _origin=None: ask_claude(
            prompt, cwd, session_id, wait_seconds, _origin=_origin),
        wants_origin=True,
        parameters=_params({
            "prompt": {"type": "string", "description": "The task: goal, constraints, what to report back"},
            "cwd": {"type": "string", "description": "Absolute folder to work in (a local repo, the KB…); empty = scratch. Ignored when continuing: a session keeps its folder"},
            "session_id": {"type": "string", "description": "Continue this Claude session (from list_claude_sessions); empty = a new one"},
            "wait_seconds": {"type": "integer", "description": "How long to wait for the answer before returning (default 240, max 900)"},
        }, ["prompt"]),
    ),
    _ToolDef(
        name="list_claude_sessions",
        description=(
            "List the Claude Code sessions on this machine — the ones meeseek started and the ones the user "
            "started in a terminal — newest first, with folder, branch and when each was last active. Use it "
            "to find the session the user means (\"the refactor from yesterday\") before continuing it with "
            "ask_claude(session_id=…)."
        ),
        fn=lambda query="", limit=10: claude_sessions(query, limit),
        parameters=_params({
            "query": {"type": "string", "description": "Words to match in the title, first prompt, folder or branch"},
            "limit": {"type": "integer", "description": "How many (default 10, max 30)"},
        }),
    ),
    _ToolDef(
        name="search_meeseek_sessions",
        description=(
            "Search meeseek's OWN past conversations — the console's chats, messages from the phone, Inbox "
            "items' turns, MCP callers' requests — by what the user and meeseek said in them, best match first. "
            "Use it when the user means an earlier meeseek conversation (\"the review we did for Ovien\", "
            "\"what did you tell me about the gateway last week\"); for Claude Code sessions use "
            "list_claude_sessions. The conversation you are in is left out."
        ),
        fn=lambda query="", days=30, limit=8, _origin=None: search_meeseek_sessions(
            query, days=float(days or 30), limit=int(limit or 8), _origin=_origin),
        parameters=_params({
            "query": {"type": "string", "description": "Words that were said in it: a name, an MR number, a ticket key, a topic. Every word must appear. Empty lists the latest."},
            "days": {"type": "number", "description": "How far back (default 30, max 180)"},
            "limit": {"type": "integer", "description": "How many (default 8, max 20)"},
        }),
        wants_origin=True,
    ),
    _ToolDef(
        name="get_meeseek_session",
        description=(
            "Read one past meeseek conversation found with search_meeseek_sessions: what the user asked, what "
            "meeseek answered and which tools it used, the last `turns` exchanges — to recall what was "
            "concluded, or carry on from it."
        ),
        fn=lambda session_id, turns=20, _origin=None: get_meeseek_session(session_id, turns=int(turns or 20),
                                                                          _origin=_origin),
        parameters=_params({
            "session_id": {"type": "string", "description": "The session id from search_meeseek_sessions"},
            "turns": {"type": "integer", "description": "How many of the latest exchanges (default 20, max 60)"},
        }, ["session_id"]),
        wants_origin=True,
    ),
    _ToolDef(
        name="check_claude",
        description=(
            "Where a Claude session meeseek started stands — still working, or done with its answer. "
            "Optionally wait up to `wait_seconds` for it to finish."
        ),
        fn=lambda session_id, wait_seconds=0: claude_check(session_id, wait_seconds),
        parameters=_params({
            "session_id": {"type": "string"},
            "wait_seconds": {"type": "integer", "description": "Wait this long for it to finish (default 0, max 900)"},
        }, ["session_id"]),
    ),
    # --- Seeing what was built (a plugin: Gemini looks at a rendered page) ---
    _ToolDef(
        name="review_page",
        description=(
            "Have Gemini look at a web page you built (an HTML file under outputs/) rendered at phone "
            "(500px) and desktop (1280px) width, and list what a person would find broken or unreadable — "
            "content cut off, overlap, an empty chart, placeholder text, low contrast — by severity, with a "
            "fix for each. Call it after building or changing a page; fix the high and medium ones and call "
            "it again (twice at most). You cannot see the page yourself; this is how you check it."
        ),
        fn=lambda path, purpose="": review_page(path, purpose),
        parameters=_params({
            "path": {"type": "string", "description": "The page: an absolute path, or one relative to outputs/ (e.g. pages/report.html)"},
            "purpose": {"type": "string", "description": "What the page is for, in a sentence — what it is checked against"},
        }, ["path"]),
    ),
    # --- Image generation (general tool: attached to every skill) ---
    _ToolDef(
        name="generate_image",
        description=(
            "Generate a picture with Gemini's image model and save it as a local PNG — for "
            "APPEARANCE: characters/NPCs, scenes, props, UI mock-ups, mood, storyboards (relations "
            "and flows → render_diagram). Draw without asking when the user asks for a picture, "
            "attaches one to change, or the deliverable needs one (character sheet, UI proposal); "
            "in an open discussion offer one in a sentence instead. At most 3 per turn. For the "
            "same character or world, pass the previous image in reference_images and describe "
            "only what changes. The prompt is sent to Vertex AI: describe the picture, never "
            "confidential specs, internal URLs or real people; no real people, trademarks, violence "
            "or sexual content. Copy the returned `![...](...)` line into your reply unchanged."
        ),
        fn=lambda prompt, aspect_ratio="1:1", reference_images=None, use_attached_images=False,
        _attached_images=None: generate_image(
            prompt, aspect_ratio, reference_images, use_attached_images, _attached_images=_attached_images),
        parameters=_params({
            "prompt": {"type": "string", "description": "What the image should show: subject, style, composition, mood. With references, say how to use them (same character / this style / this palette)."},
            "aspect_ratio": {"type": "string", "description": "1:1 (default), 3:4, 4:3, 9:16, 16:9, 2:3, 3:2, 4:5, 5:4, 21:9"},
            "reference_images": {"type": "array", "items": {"type": "string"}, "description": "Local image file paths to use as references (e.g. a previous generation under knowledge_base/outputs/images/, a character sheet). Max 6."},
            "use_attached_images": {"type": "boolean", "description": "true to also use the images the user attached to this turn as references."},
        }, ["prompt"]),
        wants_user_images=True,
    ),
    # --- Web ---
    _ToolDef(
        name="fetch_url",
        description=("Fetch a public web page and return its readable text. Pair with google_search: search to find URLs, "
                     "then fetch_url to actually read the promising ones. Google Docs/Sheets/Slides/Drive links are read "
                     "with the user's own Google login (every tab of a sheet; a long file is saved for kb_read). "
                     "http(s) only; internal/private hosts are blocked."),
        fn=fetch_url,
        parameters=_params({
            "url": {"type": "string", "description": "An http(s) URL to read, e.g. from a google_search result, or a Google Docs/Sheets/Drive link"},
        }, ["url"]),
    ),
    # --- Tasks: a goal that outlives the turn (runtime/tasks.py) ---
    _ToolDef(
        name="task_create",
        description=(
            "開一個跨 turn 追蹤的任務：目標與進度由 engine 保存，每一輪都放進 context，"
            "並可定期喚醒一輪來推進。只在使用者於 console 明確要求持續追蹤某件事時使用"
            "（「幫我追這個 MR 到合併」）；goal 是使用者同意的目標，success 是怎樣算完成，"
            "開之前先跟使用者確認這兩句。觸發型 turn（Inbox、MCP）不能開任務。"
        ),
        fn=lambda goal, success, first_check_hours=24, _origin=None: task_create(
            goal, success, float(first_check_hours or 0), _origin=_origin),
        wants_origin=True,
        parameters=_params({
            "goal": {"type": "string", "description": "使用者同意的目標，一句話（例：追 MR !169 到合併）"},
            "success": {"type": "string", "description": "成敗條件，一句話（例：MR !169 已 merge 到 main）"},
            "first_check_hours": {"type": "number", "description": "幾小時後自動喚醒第一次檢查（預設 24；最小 0.25，更小會夾到 0.25；0 = 不排）", "default": 24},
        }, ["goal", "success"]),
    ),
    _ToolDef(
        name="task_note",
        description=(
            "在任務上記一筆，給下一輪的自己看：progress 記一個做完且確認有效的步驟"
            "（append、不會被丟掉）；note 記嘗試與觀察（試了什麼、結果如何；只保留最近 "
            "10 筆）。每次推進任務後都要記——下一輪只看得到這裡寫過的。"
        ),
        fn=lambda task_id, note="", progress="": task_note(task_id, note, progress),
        parameters=_params({
            "task_id": {"type": "string", "description": "context 任務區塊裡的 [id]"},
            "note": {"type": "string", "description": "工作筆記：試過什麼、效果如何", "default": ""},
            "progress": {"type": "string", "description": "一個成功、確定算數的步驟", "default": ""},
        }, ["task_id"]),
    ),
    _ToolDef(
        name="workstream_get",
        description=(
            "Read the shared notes on one piece of work — a ticket, an MR or a branch: its goal, "
            "where it stands, the decisions made, open questions, the latest events, and the "
            "meeseek conversations and Claude Code sessions that worked on it. Read it before "
            "starting on a ticket or MR, instead of asking the person to recap."
        ),
        fn=lambda key="", branch="": workstream_get(key, branch),
        parameters=_params({
            "key": {"type": "string", "description": "A ticket key (e.g. PROJ-123) or an MR link or !123", "default": ""},
            "branch": {"type": "string", "description": "A git branch, when there is no key", "default": ""},
        }, []),
    ),
    _ToolDef(
        name="workstream_note",
        description=(
            "Add one line to a piece of work's shared notes — a decision, a result, what is "
            "next — so the next meeseek turn or Claude Code session on it knows. Use it when "
            "something about the ticket or MR changed; not for chatter."
        ),
        fn=lambda key, text, _origin=None: workstream_note(key, text, _origin=_origin),
        wants_origin=True,
        parameters=_params({
            "key": {"type": "string", "description": "The ticket key, MR link or !123 the note is about"},
            "text": {"type": "string", "description": "One line: what changed"},
        }, ["key", "text"]),
    ),
    # --- The goldfish review of a spec (runtime/goldfish.py) ---
    _ToolDef(
        name="review_spec",
        description=(
            "A zero-context review of a PRD, design or spec before you ship it: three reviewers that "
            "see only the document and the files and links it names — comprehension (can it be "
            "restated unaided), zoom-out (simplicity, YAGNI, reuse), implementation readiness (paths, "
            "signatures, tests). Returns findings by severity: blocker, major, nit. Call it on the "
            "finished draft before ship_output. Blockers: fix them, then call it once more on the "
            "whole revised document (the one re-review). What remains goes into the document as an "
            "'Accepted trade-offs' section. Costs three model calls a review. If it says the review "
            "did not run, tell the user; never claim the document was reviewed."
        ),
        fn=lambda content="", path="", _origin=None: review_spec(content, path, _origin=_origin),
        wants_origin=True,
        parameters=_params({
            "content": {"type": "string", "description": "The whole document, as Markdown", "default": ""},
            "path": {"type": "string", "description": "Or a knowledge-base path (as kb_list shows it), when the document is saved", "default": ""},
        }, []),
    ),
    _ToolDef(
        name="task_update",
        description=(
            "改任務狀態或安排下次檢查。status：active / waiting（等外部事件）/ done / "
            "dropped——done 和 dropped 只開放在使用者的 console session（達成成功條件時設 "
            "done 並回報使用者）。next_check_hours 安排幾小時後自動喚醒一輪來推進任務"
            "（取代原本排定的檢查）。"
        ),
        fn=lambda task_id, status="", note="", next_check_hours=0, _origin=None: task_update(
            task_id, status, note, float(next_check_hours or 0), _origin=_origin),
        wants_origin=True,
        parameters=_params({
            "task_id": {"type": "string", "description": "context 任務區塊裡的 [id]"},
            "status": {"type": "string", "description": "active、waiting、done 或 dropped；留空表示不改", "default": ""},
            "note": {"type": "string", "description": "順便記一筆筆記（同 task_note 的 note）", "default": ""},
            "next_check_hours": {"type": "number", "description": "幾小時後喚醒下一次檢查（最小 0.25，更小會夾到 0.25）；0 = 不改", "default": 0},
        }, ["task_id"]),
    ),
    # --- Reminders: push a line to the user's phone later (runtime/wakes.py) ---
    _ToolDef(
        name="remind_me",
        description=(
            "排一個提醒：時間到了把 text 原樣推到使用者的 Telegram——不起 agent turn、"
            "不吃每日自主 turn 預算。使用者本人說「五點提醒我」「15 分鐘後叫我」就用它，"
            "console 和手機上的對話都可以。when 收相對時間「15m」「2h」（單位 s/m/h/d）"
            "或絕對時間「17:00」（今天；已過就是明天）、「2026-10-28 09:30」（過去的時間會"
            "拒絕）。落在安靜時段（triggers.quiet_hours）的提醒等時段結束才推。"
        ),
        fn=lambda when, text, _origin=None: remind_me(when, text, _origin=_origin),
        wants_origin=True,
        parameters=_params({
            "when": {"type": "string", "description": "什麼時候：「15m」「2h」，或「17:00」「2026-10-28 09:30」（本地時間）"},
            "text": {"type": "string", "description": "到時推給使用者的那句話，照使用者的意思寫"},
        }, ["when", "text"]),
    ),
]

_INDEX: dict[str, _ToolDef] = {td.name: td for td in _ALL_TOOLS}

# Tools attached to every skill agent regardless of its SKILL.md `tools:` line
# (runtime/skills.py appends them): everything that cannot change state outside
# this process, plus the local-artifact writers. For reads, SKILL.md `tools:`
# names the tools the SOP is written around, not a permission list.
GENERAL_TOOLS: tuple[str, ...] = tuple(
    sorted(td.name for td in _ALL_TOOLS if not td.writes)
)

# Tools that DO change external state: never auto-attached. A skill must declare
# them, and tests/unit/test_security_policy.py::SKILL_EXTERNAL_WRITE_GRANTS says
# which skill may hold which.
GATED_TOOLS: tuple[str, ...] = tuple(
    sorted(td.name for td in _ALL_TOOLS if td.writes)
)

# Tools whose result is raw evidence the reply shows (runtime/evidence.py).
RAW_TOOLS: frozenset[str] = frozenset(td.name for td in _ALL_TOOLS if td.shows_raw)

# Name prefixes of tools that only read. MCP tools carry no `writes` flag, so
# this is how the security policy and the harness tell their reads from writes.
READ_PREFIXES: tuple[str, ...] = (
    "kb_", "get_", "list_", "search_", "check_", "describe_", "query_", "k8s_status", "review_context", "review_page",
    "review_spec",
    "fetch_", "render_", "jira_search", "jira_get",
    "confluence_search", "confluence_get", "workstream_get",
)

# Tools that produce a picture (`writes=False`, a local PNG under outputs/).
# Headless runs (the morning plan) have no viewer, so they opt out with
# `read_tools(media=False)`.
MEDIA_TOOLS: tuple[str, ...] = ("generate_image",)

# Tools that hand work to Claude Code: for a turn with a person on the other
# end, never for a headless run (nobody would see the answer), so they ride the
# same opt-out as media.
CLAUDE_TOOLS: tuple[str, ...] = ("ask_claude", "list_claude_sessions", "check_claude")

# meeseek's own past conversations (runtime/past_sessions.py): for meeseek's
# agents, never a plugin. The MCP endpoint has no auth and keeps each caller's
# history apart; a plugin reading every session would undo both.
SESSION_TOOLS: tuple[str, ...] = ("search_meeseek_sessions", "get_meeseek_session")

# Tasks that outlive the turn (runtime/tasks.py): for meeseek's agents — the
# orchestrator attaches them explicitly (it holds no read tools) and skill
# agents get them with the other reads — never a plugin: an MCP caller has no
# console session, so it could neither open nor settle one.
TASK_TOOLS: tuple[str, ...] = ("task_create", "task_note", "task_update")

# Reminders (runtime/wakes.py): the user's own "叫我" from the console or the
# phone — for meeseek's agents, never a plugin: an MCP caller is not the
# person, and its push would land on their phone unasked.
REMIND_TOOLS: tuple[str, ...] = ("remind_me",)

# The goldfish review (runtime/goldfish.py): three model calls on meeseek's
# models — for meeseek's agents, never a plugin, so an MCP caller cannot spend
# them unbounded.
SPEC_TOOLS: tuple[str, ...] = ("review_spec",)


# ---------------------------------------------------------------------------
# Public API — what the engine and the MCP server build their tools from
# ---------------------------------------------------------------------------


def read_tool_names(*, media: bool) -> list[str]:
    """The names of every read tool (for the SKILL.md loader and tool overview)."""
    return [n for n in GENERAL_TOOLS if media or n not in MEDIA_TOOLS + CLAUDE_TOOLS]


def specs() -> list:
    """Every registry tool as a core ToolSpec: what the turn runner and the
    MCP packs speak (core/tools.py). A write's target is not declared here
    yet; a tool moved to @tool declares it."""
    from meeseek.core.tools import Effect, ToolSpec

    return [ToolSpec(name=td.name, description=td.description, fn=td.fn, parameters=td.parameters,
                     effect=Effect.WRITE if td.writes else Effect.READ, shows_raw=td.shows_raw)
            for td in _ALL_TOOLS]


class RegistryTools:
    """The registry as a ToolProvider (ports.py)."""

    def tools(self) -> list:
        return specs()


def tool_names() -> list[str]:
    """Every registered tool name, sorted."""
    return sorted(_INDEX)


_PY_TYPES = {"string": str, "integer": int, "number": float,
             "boolean": bool, "array": list, "object": dict}
_TYPE_DEFAULTS = {str: "", int: 0, float: 0.0, bool: False}


def signature_params(parameters: dict) -> list[inspect.Parameter]:
    """A JSON-schema parameter block as Python parameters, so a schema built
    from the signature (FastMCP's) agrees with the declared one."""
    props = parameters.get("properties", {})
    required = parameters.get("required", [])
    out = []
    for name, spec in props.items():
        annotation = _PY_TYPES.get(spec.get("type", "string"), str)
        default = (inspect.Parameter.empty if name in required
                   else spec.get("default", _TYPE_DEFAULTS.get(annotation)))
        out.append(inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                     default=default, annotation=annotation))
    return out


def tool_specs(*names: str) -> list[tuple[str, str, Callable[..., str], dict]]:
    """(name, description, fn, parameters) for the named tools.

    For consumers that build their own wrappers (the MCP server). Raises
    KeyError for an unknown name so a typo fails at startup.
    """
    out = []
    for name in names:
        if name not in _INDEX:
            raise KeyError(f"Unknown tool name: {name!r} (not in registry)")
        td = _INDEX[name]
        out.append((td.name, td.description, td.fn, td.parameters))
    return out


