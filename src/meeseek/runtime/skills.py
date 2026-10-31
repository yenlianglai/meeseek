"""Skill loader: every directory under skills/ is one routed agent.

    skills/<name>/
        SKILL.md     # required: frontmatter (metadata) + body (the SOP)
        tools.py     # optional: skill-owned tools (module-level TOOLS list)
        learned.md   # runtime: consented learned rules, read every turn

SKILL.md format:

    ---
    description: What this skill does — the router uses this to delegate.
    model: workflow              # tier alias (router|workflow|fast|coding, see
                                 #   project.yaml models:) or a literal model id
    tools: comment_mr, create_mr   # WRITE tools only — reads reach every agent
    mcp: true                    # bind MCP servers declared for this skill in project.yaml
    ship: debug_reports          # inject ship_output → outputs/<category>/
    limits:                      # per-tool argument caps, clamped before each call
      list_timeseries: {pageSize: 100}   # quote a value for a string-typed param
    routes:                      # lines the router's prompt lists: what it sends here
                                 # beyond the description (a link's shape, a screenshot)
    search_limits:               # per-turn call counts: past N the result carries a
      kb_grep: 6                 #   "converge and answer" hint; past 2N the call is blocked
    ---
    Procedure:
    1. ...

Output and call limits, one concern each:

    1. Tool windows (kb_read / get_file: 24k chars) — cut at a line boundary,
       with a footer saying which lines came back and where to continue.
    2. `limits:` — argument caps, enforced by before_tool_callback.
    3. `search_limits:` — per-turn call counts (hint at N, block after 2N).
    4. `models.iteration_budget` (global) — total tool calls per turn; when spent,
       the model gets one grace call to answer.
    5. History compaction (`models.compaction_chars`) — the middle of a long
       history is summarised, keeping the last `compaction_keep` messages.

Skill-owned tools (tools.py TOOLS) attach without being declared.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from meeseek.config.paths import home
from meeseek.context import prompts
from meeseek.context.prompts import render
from meeseek.context.outputs import save_session

logger = logging.getLogger(__name__)

_BUILTIN_TOOLS = ("google_search",)


@dataclass(slots=True)
class Skill:
    """A parsed skill definition (SOP + attached tools + ship target)."""

    name: str
    path: Path
    raw_description: str
    body: str
    model: str = "workflow"
    tools: list[str] = field(default_factory=list)
    use_mcp: bool = False
    ship_categories: list[str] = field(default_factory=list)
    tool_limits: dict[str, dict] = field(default_factory=dict)
    call_limits: dict[str, int] = field(default_factory=dict)  # per-turn advisory caps per tool
    routes: list[str] = field(default_factory=list)  # what the router sends here beyond the description

    @property
    def description(self) -> str:
        """Rendered when read: a pack's placeholders fill in once it is running."""
        return render(self.raw_description)

    @property
    def agent_name(self) -> str:
        # Agent names are tool-enum values: Python identifiers.
        return f"{self.name.replace('-', '_')}_agent"

    @staticmethod
    def _to_agent_name(skill_name: str) -> str:
        return f"{skill_name.replace('-', '_')}_agent"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split SKILL.md into (frontmatter dict, body)."""
    if not text.startswith("---"):
        return {}, text
    _, fm, body = text.split("---", 2)
    return yaml.safe_load(fm) or {}, body.strip()


def _parse_tool_list(value) -> list[str]:
    """Accept `a, b, c` (string) or a YAML list — used for tools and ship."""
    if not value:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    return [str(t).strip() for t in value]


# Every key a SKILL.md may declare. A skill's name is its directory name.
_KNOWN_KEYS = frozenset({
    "description", "model", "tools", "mcp", "ship", "limits", "search_limits", "routes",
})


def _check_frontmatter(skill_name: str, meta: dict, tools: list[str]) -> None:
    """Refuse unknown frontmatter keys, and read tools in `tools:` (which
    grants writes only)."""
    from meeseek.tools.registry import read_tool_names

    unknown = sorted(set(meta) - _KNOWN_KEYS)
    if unknown:
        raise ValueError(
            f"skills/{skill_name}/SKILL.md: unknown frontmatter key(s) {unknown}. "
            f"Valid keys: {sorted(_KNOWN_KEYS)}. "
            "(A skill's name is its directory name, not a frontmatter field.)")

    reads = sorted(set(tools) & set(read_tool_names(media=True)))
    if reads:
        raise ValueError(
            f"skills/{skill_name}/SKILL.md: `tools:` lists read tool(s) {reads}. "
            "`tools:` grants WRITE tools only; every agent already gets every read "
            "tool. Delete them from the frontmatter — if the SOP needs to say which "
            "reads matter for this job, say it in the body.")


def _parse_skill(skill_dir: Path) -> Skill:
    meta, body = _split_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    tools = _parse_tool_list(meta.get("tools"))
    _check_frontmatter(skill_dir.name, meta, tools)
    return Skill(
        name=skill_dir.name,
        path=skill_dir,
        raw_description=str(meta.get("description", "")).strip(),
        body=body,
        model=meta.get("model", "workflow"),
        tools=tools,
        use_mcp=bool(meta.get("mcp", False)),
        ship_categories=_parse_tool_list(meta.get("ship")),
        tool_limits=dict(meta.get("limits") or {}),
        call_limits={k: int(v) for k, v in (meta.get("search_limits") or {}).items()},
        routes=[str(r).strip() for r in meta.get("routes") or [] if str(r).strip()],
    )


def discover_skills(skills_dir: Path | None = None) -> list[Skill]:
    """Parse every skills/*/SKILL.md (sorted by name)."""
    base = skills_dir or home().skills
    if not base.exists():
        return []
    return [
        _parse_skill(d)
        for d in sorted(base.iterdir())
        if d.is_dir() and (d / "SKILL.md").exists()
    ]


def _load_local_tools(skill: Skill) -> dict:
    """Import skills/<name>/tools.py and index its TOOLS by function name."""
    tools_file = skill.path / "tools.py"
    if not tools_file.exists():
        return {}
    module_name = f"skills.{skill.name.replace('-', '_')}.tools"
    spec = importlib.util.spec_from_file_location(module_name, tools_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = getattr(module, "TOOLS", None)
    if tools is None:
        raise AttributeError(f"skills/{skill.name}/tools.py must define a TOOLS list")
    return {fn.__name__: fn for fn in tools}


def _make_ship_tool(skill: Skill):
    """The ship_output tool for this skill's output categories. An unknown
    category falls back to the first: it only picks a folder, so a wrong guess
    must not fail the shipment."""
    categories = skill.ship_categories
    default = categories[0]
    choices = ", ".join(categories)

    def ship_output(topic: str, content: str, category: str = "") -> str:
        """Ship the final deliverable: persist the completed report/analysis as a Markdown file.

        Call this once at the end of a completed task with the full result.
        Do NOT call it for short conversational answers.

        Args:
            topic: Short topic for the filename (e.g. "login_timeout_debug")
            content: The complete deliverable in Markdown
            category: Which kind of deliverable this is. One of: CHOICES.
        """
        chosen = category if category in categories else default
        path = save_session(content, category=chosen, topic=topic)
        return f"Shipped -> {home().rel(path)}"

    ship_output.__doc__ = ship_output.__doc__.replace("CHOICES", choices)
    return ship_output


def common() -> str:
    """What every agent — the router and each skill — is told, after its own role."""
    return "\n\n" + prompts.text("blocks/common").strip()


