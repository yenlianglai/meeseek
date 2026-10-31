/** Slash commands the server dispatches deterministically, fetched from the
 *  backend (/slash-commands) so the palette lists exactly what the server runs. */

export interface SlashCommand {
  name: string;
  args: string; // argument hint, empty if none
  description: string;
  /** Runs an orchestrator tool inside before_model_callback, so the run emits
   *  no SSE events until it finishes. Commands that transfer to a sub-agent
   *  stream their tool calls as they happen. */
  long_running: boolean;
  /** Offered as a starting point in the chat empty state. */
  featured: boolean;
}

let commands: SlashCommand[] = [];
let pending: Promise<SlashCommand[]> | null = null;

async function fetchCommands(): Promise<SlashCommand[]> {
  try {
    const res = await fetch("/slash-commands");
    if (!res.ok) throw new Error(String(res.status));
    const rows = await res.json();
    if (Array.isArray(rows)) commands = rows as SlashCommand[];
  } catch {
    // The failure is not cached: clearing `pending` lets the next
    // loadCommands() retry, so a backend that was not up yet is picked up the
    // next time the chat empty state mounts. Nothing polls.
    pending = null;
  }
  return commands;
}

/** Load the palette. A failure leaves it empty; typing a command still runs it.
 *  The in-flight promise is shared, so the app shell and the chat empty state
 *  make one request between them. */
export async function loadCommands(): Promise<SlashCommand[]> {
  pending ??= fetchCommands();
  return pending;
}

/** The commands the server marked as worth offering first, in server order. */
export function featuredCommands(): SlashCommand[] {
  return commands.filter((c) => c.featured);
}

/** The row for a draft like "  /plan tomorrow" when that command runs as an
 *  orchestrator tool; null otherwise.
 *
 *  Parsing mirrors _SLASH_RE in runtime/slash_commands.py. Python's `\w` is
 *  Unicode-aware, hence the property escapes rather than JS's ASCII `\w` —
 *  otherwise "/plan計畫" would parse as "plan" here and "plan計畫" there. */
export function longRunningCommand(draft: string): SlashCommand | null {
  const m = /^\/([\p{L}\p{N}_]+)/u.exec(draft.trim());
  if (!m) return null;
  const name = m[1].toLowerCase();
  return commands.find((c) => c.name === name && c.long_running) ?? null;
}

/** Commands matching a draft like "/fac" — active only while typing the name. */
export function matchCommands(draft: string): SlashCommand[] {
  if (!draft.startsWith("/")) return [];
  const head = draft.slice(1);
  if (head.includes(" ") || head.includes("\n")) return []; // already past the name
  const q = head.toLowerCase();
  return commands.filter((c) => c.name.startsWith(q));
}
