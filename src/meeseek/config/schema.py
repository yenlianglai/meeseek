"""Configuration schema — pure dataclasses with no side effects.

**This file is where a default value lives.** The loader adds nothing of its
own (tests/unit/test_config.py pins that). Derivations between fields
(`coding` follows `workflow`) are `__post_init__` rules here for the same
reason: `Config()` built directly gives the same answer as the loader.

Constructing these must stay free of side effects: exporting values to the
environment is `loader.build_config`'s job.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "CoachMeeting",
    "Config",
    "GoogleCloudConfig",
    "LearningConfig",
    "MCPServerConfig",
    "MediaConfig",
    "ModelsConfig",
    "ProjectConfig",
    "TriggersConfig",
    "MessengersConfig",
    "ClaudeConfig",
    "UsageConfig",
    "WorkflowsConfig",
]


@dataclass
class ProjectConfig:
    name: str = ""
    team_name: str = ""
    # Teams/domains referenced when attributing speakers or requirement sources
    # (e.g. in meeting-note transcription). Empty = the pipeline just uses the
    # team_name and a generic "[participant]" label.
    teams: list[str] = field(default_factory=list)


@dataclass
class GoogleCloudConfig:
    """Vertex AI + managed-MCP quota settings — set as env vars by the loader."""
    project: str = ""          # GOOGLE_CLOUD_PROJECT (Vertex AI)
    location: str = "global"   # GOOGLE_CLOUD_LOCATION
    quota_project: str = ""    # GCP_PROJECT_ID (managed MCP x-goog-user-project)
    vertex: bool = True        # GOOGLE_GENAI_USE_VERTEXAI


@dataclass
class MediaConfig:
    """Generated pictures as a medium of the conversation; when to draw is in the picture tools' descriptions (tools/registry.py)."""
    # Soft per-turn cap (make_call_cap warns at the limit, blocks at 2x): a
    # three-frame storyboard is reasonable, a gallery is not.
    image_calls_per_turn: int = 3


@dataclass
class UsageConfig:
    """The Usage page (runtime/usage.py). Costs are list prices from litellm's
    price table, in US$; this rate shows them in NT$ too — set it to the rate
    you budget with."""
    usd_to_twd: float = 32.0


@dataclass
class LearningConfig:
    """How meeseek learns from its own use (runtime/trajectory.py, reward.py,
    improve.py): what it keeps and what the weekly improvement run may spend.
    Every change it finds waits for your approval."""
    session_days: int = 180     # conversations kept in sessions.db (the past-sessions tools read 180 days)
    weekly_usd: float = 5.0     # the weekly run's budget, every model call it makes included
    max_replays: int = 40       # replays a week, whatever they cost: they read real systems
    k: int = 2                  # candidate edits a week


@dataclass
class WorkflowsConfig:
    meeting_notes: bool = True
    # What /plan reads before it plans the day: [{label, tool, args}], each a
    # tool meeseek or a pack serves. In a string argument {days} is how far
    # back to look: 3 on a Monday (the weekend), else 1.
    daily_plan: list[dict] = field(default_factory=list)


@dataclass
class CoachMeeting:
    """A meeting the coach takes on: its name as on the calendar (a title
    containing it, case and spacing aside), and what to focus on there — a
    document to work through, say — when it is not the usual round."""
    name: str
    focus: str = ""


@dataclass
class TriggersConfig:
    """Events that start work on their own (runtime/triggers.py). Each source
    is polled; what passes its filter is triaged by the fast model, and what
    needs you becomes an Inbox item an agent works on. The agent only reads:
    replies are drafts you send yourself, and every write waits for your yes."""
    # Autonomous turns a day, all sources together (reply / investigate /
    # brief). Past the cap an item is still filed — as fyi, without a turn,
    # runnable from the Inbox by hand. 0 = no cap.
    daily_turns: int = 40
    # When the Inbox digest goes to the phone (cron, local time): what waits
    # unseen — finished answers, held writes, open questions — summed up, with
    # 全部標為已讀. Silent when nothing changed, or nothing waits. "" = off.
    digest_cron: str = "0 10,16 * * 1-5"
    # When the daily review runs (cron, local time): the last 24 hours' log
    # warnings, failed turns, corrections and Inbox failures, handed to Claude
    # Code for causes and ranked fixes — one Inbox item, each fix approvable as
    # a Claude session on a new branch (runtime/daily_review.py). Nothing
    # filed on a day with nothing new. "" = off.
    review_cron: str = "30 18 * * 1-5"
    # Local hours Telegram's own pushes sit out, e.g. "23-8" (11pm–8am): a
    # proposal, coach report or Claude result waits until the hours end and is
    # sent then, once. Replies to a message you just sent go out regardless.
    # Empty = no quiet hours.
    quiet_hours: str = ""
    # The sources that file items: {name: {tool, args, every_s}} — a pack's
    # source tool polled every `every_s` seconds (default: each minute), with
    # `args`. An item filed from one says `source: <name>`.
    sources: dict[str, dict] = field(default_factory=dict)
    # Record Chrome's audio and your microphone while a Google Meet tab is open
    # (macOS, recorder/), then turn the recording into meeting notes.
    record_meetings: bool = False
    # Coach you in these meetings (runtime/meeting_coach.py): about to start, a
    # rehearsal instead of a brief; recorded, a report on how you came across,
    # with its transcript and recording. Each is a name, or {name, focus}.
    # Empty: no coaching.
    coach_meetings: list[CoachMeeting] = field(default_factory=list)
    # People whose moves the coach's report watches for in "向他人學習" — how
    # they chair, pace, give direction, phrase and reason — named as they
    # appear in transcripts. The report learns from anyone; these get special
    # attention. Empty: no special attention, the section still looks.
    coach_learn_from: list[str] = field(default_factory=list)
    # Where the rehearsals and the coach's reports also go: a Telegram chat id
    # (your own chat with the bot is your user id). Only this chat — they are
    # personal. Empty: the Inbox only.
    coach_telegram_chat: str = ""
    # A recorded meeting's action items as Jira work (runtime/meeting_tickets.py):
    # yours and your team's, each searched in Jira first — a comment on the
    # ticket that has it, a new ticket only for one of yours that none has —
    # held on the meeting's item for your yes, one by one. Keys: project (the
    # Jira key new tickets go to; "" = off), components, issue_type ("Task"),
    # summary_prefix (e.g. "[Team] "), assignee (your Jira login, for your
    # own new tickets), team (who your team is, e.g. "Casey, Sasha"),
    # create_for_team (also new tickets for your team's items; default no),
    # create_tool / comment_tool (the write tools: jira_create_issue,
    # jira_add_comment) and search_tool (jira_search).
    meeting_tickets: dict = field(default_factory=dict)


@dataclass
class ClaudeConfig:
    """Claude Code as an agent meeseek hands work to (runtime/claude_agent.py),
    on this machine's Claude Code login. It works in the folder it is given, in
    Claude Code's auto mode, with meeseek's plugins (context lookups, drawing,
    review_page) and nothing else of meeseek's: it writes to no team system,
    and nothing waits on a person."""
    enabled: bool = True
    # meeseek's MCP server: where Claude reaches the plugins; empty = none.
    meeseek_mcp_url: str = "http://127.0.0.1:8787/mcp-server/mcp"
    model: str = ""                    # empty = Claude Code's default
    max_turns: int = 200               # tool round-trips per message; 0 = no cap
    page_review_model: str = ""        # review_page's eyes; empty = models.multimodal


@dataclass
class MessengersConfig:
    """Talk to the orchestrator from your phone (runtime/messengers/). A message
    is an Inbox item: the answer comes back to the chat, and a write it wants
    waits for your Approve there or on the Inbox page. Only the user ids listed
    are answered — with none listed, the bot replies with the sender's id so you
    can add yours. What the agents answer passes through Telegram's servers;
    the bot token is in .env (TELEGRAM_BOT_TOKEN)."""
    telegram: bool = False          # long polling: no public URL needed
    telegram_allowed_users: list[str] = field(default_factory=list)  # Telegram user ids (numbers)


@dataclass
class ModelsConfig:
    router: str = "gemini-2.5-pro"
    workflow: str = "gemini-2.5-pro"
    fast: str = "gemini-2.5-flash"
    multimodal: str = "gemini-2.5-flash"
    # Tier for code-heavy skills (code review and implementation). Empty = the workflow
    # model; point it at any litellm-supported id (e.g. a Bedrock Claude or an
    # OpenAI model) in project.yaml without touching skills.
    coding: str = ""
    # Native image-generation model for the general generate_image tool.
    image: str = "gemini-3.1-flash-image"
    max_turns: int = 20
    # Tool calls per turn, per agent. When spent, tools return an "answer now"
    # message and the model gets one grace call.
    iteration_budget: int = 60
    # Wall-clock minutes an autonomous turn (Inbox, MCP) may run; past it tools
    # return "time is up, answer now". Call budgets cannot see time spent
    # waiting (model retries, a slow MCP server). 0 = no limit; turns a person
    # watches in the console are never time-capped.
    turn_minutes: int = 15
    # A turn that dies on a transient error (429, 5xx, timeout, dropped
    # connection) resumes from its last event, up to this many times.
    transient_retries: int = 10
    # History compaction: once the request history exceeds this many chars,
    # the middle is summarised (models.fast) and the last `compaction_keep`
    # contents are kept verbatim. 0 disables.
    compaction_chars: int = 240_000
    compaction_keep: int = 12
    # An OpenAI-compatible endpoint every model call goes through (a gateway
    # such as GoModel or a litellm proxy: one place for keys, routing, retries
    # and cost). Empty = call each provider directly. Its key: MODEL_GATEWAY_KEY.
    gateway: str = ""

    def __post_init__(self) -> None:
        self.coding = self.coding or self.workflow

    def resolve(self, tier_or_id: str) -> str:
        """A SKILL.md `model:` value → a real model id.

        Skills name a tier (`fast`, `coding`, …) so the ids can be retuned in one
        place; anything that is not a tier name is passed through as a literal
        model id. Empty means the workflow tier.
        """
        tier = getattr(self, tier_or_id, None) if tier_or_id in _MODEL_TIERS else None
        return tier or (tier_or_id if tier_or_id else self.workflow)


_MODEL_TIERS = ("router", "workflow", "fast", "multimodal", "coding", "image")


@dataclass
class MCPServerConfig:
    name: str = ""
    # a pack from packs/ (installed beside meeseek): run as `python -m meeseek_pack_<pack>`,
    # handed `config` as JSON in MEESEEK_PACK_CONFIG; its tools say whether they write
    pack: str = ""
    config: dict = field(default_factory=dict)
    # stdio transport (local servers)
    command: str = ""
    args: list[str] = field(default_factory=list)
    args_from_env: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # streamable HTTP transport (remote servers, e.g. Google-managed *.googleapis.com/mcp)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    auth: str = ""  # "adc" = Google Application Default Credentials
    impersonate: str = ""  # 選配：以此 SA 身分呼叫（permission 層降權，給唯讀 SA）
    quota_project: str = ""  # sent as x-goog-user-project; empty = google_cloud.quota_project
    # common
    tool_filter: list[str] = field(default_factory=list)
    # the skills that get its tools; "*" = every skill (a pack's default). With
    # "*", a write tool goes only to the skills whose `tools:` grant it.
    agents: list[str] = field(default_factory=list)


@dataclass
class Config:
    project: ProjectConfig = field(default_factory=ProjectConfig)
    google_cloud: GoogleCloudConfig = field(default_factory=GoogleCloudConfig)
    workflows: WorkflowsConfig = field(default_factory=WorkflowsConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    usage: UsageConfig = field(default_factory=UsageConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    triggers: TriggersConfig = field(default_factory=TriggersConfig)
    messengers: MessengersConfig = field(default_factory=MessengersConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)

