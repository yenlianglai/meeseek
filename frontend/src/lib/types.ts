/** The console's API (surfaces/console.py): sessions, and the events of their turns. */

/** A conversation as the list shows it: its title is its first message. */
export interface SessionRow {
  id: string;
  user: string; // whose: "webui" for the console's, "inbox", "mcp:<caller>", …
  title: string;
  updated: number; // epoch seconds
  running?: boolean; // a turn is running right now (watch it with attachTurn)
}

export interface Session extends SessionRow {
  events: ConsoleEvent[];
  running_turn?: string; // the running turn's id, when `running`
}

interface EventBase {
  turn: string;
  at: number; // epoch seconds
  by: string; // who: the person's surface, or the agent that answered
}

export interface CallRef {
  id: string;
  name: string;
}

/** One event of a turn, as the console reads it. */
export type ConsoleEvent =
  | (EventBase & { kind: "said"; text: string; files: { mime: string; url: string }[]; answers: string })
  | (EventBase & {
      kind: "step";
      text: string;
      calls: (CallRef & { args: Record<string, unknown> })[];
      model?: string;
      tokens?: { input: number; output: number };
    })
  | (EventBase & { kind: "result"; call: CallRef; text: string; error: boolean; held: boolean })
  | (EventBase & { kind: "reply"; text: string })
  | (EventBase & { kind: "policy"; policy: string; said: string; to_model: boolean });

/** What a turn's stream carries besides its events. */
export type StreamEvent =
  | ConsoleEvent
  | { kind: "retry"; attempt: number; of: number; in_s: number; error: string }
  | { kind: "error"; message: string };

/**
 * A file the user attached to a message — an image or a PDF. Inlined as base64
 * in the request: no upload step, so nothing has to be cleaned up if the
 * message is never sent.
 */
export interface Attachment {
  name: string;
  mimeType: string;
  data: string; // base64, no data: prefix; "" for one a past message brought (see url)
  url?: string; // where the server keeps it, for one a past message brought
}

/** UI-side conversation model, folded from the event stream. */
export interface ToolCall {
  name: string;
  args?: Record<string, unknown>;
  result?: string;
  done: boolean;
  error?: boolean; // the tool reported a failure, not a payload
}

export interface ChatMessage {
  key: string;
  role: "user" | "agent";
  author: string; // e.g. acme_app_orchestrator, sprint_agent
  text: string;
  tools: ToolCall[];
  attachments?: Attachment[]; // images/PDFs the user sent with this message
  streaming?: boolean;
  error?: string;
  turn?: string; // the turn this message belongs to
  interrupted?: boolean; // the turn stopped before its reply: it can be resumed
  notice?: string; // a transient state worth showing, e.g. an automatic retry
  question?: Question; // an ask_user call waiting for the user's answer
}

/** The arguments of an ask_user call: the turn is paused until it is answered. */
export interface Question {
  question: string;
  options: string[];
  recommended?: string;
}

/** Learning-page shapes returned by `/learning_api/*`. */
export type RuleVerdict = "working" | "redundant" | "not_followed" | "unknown" | "no_case";

export interface LearnedRule {
  skill: string;
  id: string;
  text: string;
  date: string;
  source: string;
  reason: string;
  case: { created: string; session_id: string; user_id: string; turns: string[] } | null;
  check?: RuleCheck | null; // the latest weekly review of this rule
  verdicts: RuleVerdict[]; // every review's verdict, oldest first
}

export interface RuleCheck {
  verdict: RuleVerdict;
  prompt?: string;
  with?: { follows: boolean | null; why: string };
  without?: { follows: boolean | null; why: string };
}

export interface ReviewAction {
  skill: string;
  id: string;
  text: string;
  action: "remove" | "graduate" | "stale";
  why: string;
}

export interface ArchivedRule {
  skill: string;
  id: string;
  text: string;
  why: string;
  at: string;
}

/** The weekly rule review (`runtime/rule_review.py`). */
export interface Review {
  latest: { started: string; finished?: string; rules: RuleCheck[]; actions: ReviewAction[] } | null;
  running: boolean;
  archive: ArchivedRule[];
}

export interface LearnedFact {
  text: string;
  line: string; // the raw bullet — the key for pin / forget
  date: string;
  expires: string;
  live: boolean;
}

/** A Claude Code session on this machine (`/claude_api/sessions`), for the
 *  composer's picker. */
export interface ClaudeSessionRow {
  session_id: string;
  title: string;
  first_prompt: string;
  cwd: string;
  branch: string;
  updated_at: number;
  created_at: number;
  by_meeseek: boolean;    // meeseek has driven it (it has a record)
  desktop: boolean;       // opened in Claude Desktop's Code tab (its title is Desktop's)
  archived: boolean;      // archived in Claude Desktop
  record: string;
  status: "" | "running" | "done" | "failed";
}

/** Inbox shapes returned by `/inbox_api/*` (`runtime/inbox.py`). */
export type InboxStatus =
  | "new" | "working" | "ready" | "needs_approval" | "needs_input"
  | "done" | "dismissed" | "ignored" | "ignore" | "fyi" | "failed" | "expired" | "recording" | "paused" | "recorded";

/** A write the agent wanted to make, held until you approve it. */
export interface InboxAction {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  agent: string;
  status: "pending" | "approved" | "running" | "done" | "failed" | "rejected" | "expired";
  result: string;
  at: number;
}

export interface InboxItem {
  id: string;
  source: "gmail" | "chat" | "alert" | "calendar" | "meeting" | "telegram" | "gitlab" | "jira";
  key: string;
  title: string;
  sender: string;
  link: string;
  body: string;
  meta: Record<string, unknown>;
  received_at: number;
  created_at: number;
  updated_at: number;
  status: InboxStatus;
  triage: { action?: string; summary?: string; why?: string };
  session_id: string;
  user_id: string;
  message: string;
  reply: string;
  draft: string;
  question: string;
  actions: InboxAction[];
  seen: boolean;
  error: string;
}

export interface InboxSourceStatus {
  ok: boolean;
  error: string;
  at: number;
  filed: number;
}
