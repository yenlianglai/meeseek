import { PDF } from "./attachments.ts";
import type { Attachment, ChatMessage, ConsoleEvent, StreamEvent, ToolCall } from "./types.ts";

/**
 * Fold a turn's events (surfaces/console.py) into conversation messages: the
 * person's messages, and each agent's — what it said, the tools it called with
 * their results, the question it is waiting on, its reply.
 *
 * The same for a stored session's events and a live stream: the server sends
 * both alike, a step once, a reply once as shipped.
 */
export class ConversationFolder {
  messages: ChatMessage[] = [];
  private counter = 0;
  /** Prefixes this folder's message keys. A live turn's folder starts beside
   *  the history another folder keyed, so it takes its own prefix, or the two
   *  number from m0 alike and React matches the wrong rows. */
  private prefix: string;
  constructor(prefix = "m") {
    this.prefix = prefix;
  }
  /** Each call's chip, by call id, so its result finds it whichever message it is in. */
  private calls = new Map<string, ToolCall>();
  /** The last turn an agent spoke in, and whether it replied — a turn whose
   *  last word is a tool call did not finish. */
  private lastTurn: { id?: string; ended: boolean } = { ended: true };

  private key(): string {
    return `${this.prefix}${this.counter++}`;
  }

  private current(author: string): ChatMessage {
    const last = this.messages[this.messages.length - 1];
    if (last && last.role === "agent" && last.author === author && last.streaming !== false) return last;
    const msg: ChatMessage = { key: this.key(), role: "agent", author, text: "", tools: [], streaming: true };
    this.messages.push(msg);
    return msg;
  }

  /** The agent message a stream note belongs to: the one being written, else a new one. */
  private live(): ChatMessage {
    const last = this.messages[this.messages.length - 1];
    return last && last.role === "agent" && last.streaming !== false ? last : this.current("agent");
  }

  /** The person's message, drawn as they send it (the stream does not echo it). */
  said(text: string, attachments: Attachment[] = []): void {
    this.messages.push({ key: this.key(), role: "user", author: "you", text, tools: [],
                         attachments: attachments.length ? attachments : undefined });
  }

  push(e: StreamEvent | ConsoleEvent): void {
    switch (e.kind) {
      case "retry": {
        this.live().notice = `${e.error.split(":")[0]} — retrying in ${Math.round(e.in_s)}s (${e.attempt}/${e.of})`;
        return;
      }
      case "error": {
        const msg = this.live();
        msg.error = e.message;
        msg.streaming = false;
        return;
      }
      case "said": {
        if (e.answers) {
          // An answer to a question: the question is settled.
          const last = this.messages[this.messages.length - 1];
          if (last && last.role === "agent") last.question = undefined;
        }
        let n = 0;
        const attachments: Attachment[] = e.files
          .filter((f) => f.mime.startsWith("image/") || f.mime === PDF)
          .map((f) => ({ name: f.mime === PDF ? `document ${++n}.pdf` : `image ${++n}`, mimeType: f.mime, data: "", url: f.url }));
        if (e.text.trim() || attachments.length) this.said(e.text, attachments);
        return;
      }
      case "step": {
        const msg = this.current(e.by);
        msg.turn = e.turn;
        msg.notice = undefined;
        msg.interrupted = false;
        this.lastTurn = { id: e.turn, ended: false };
        for (const c of e.calls) {
          if (c.name === "ask_user") {
            const a = c.args as { question?: string; options?: string[]; recommended?: string };
            msg.question = {
              question: String(a.question ?? ""),
              options: (a.options ?? []).map(String),
              recommended: a.recommended ? String(a.recommended) : undefined,
            };
            continue;
          }
          if (this.calls.has(c.id)) continue;
          const tool: ToolCall = { name: c.name, args: c.args, done: false };
          this.calls.set(c.id, tool);
          msg.tools.push(tool);
        }
        if (e.text) {
          // What it said with its tools stands as written; the reply comes as its own.
          msg.text = e.text;
          msg.streaming = false;
        }
        return;
      }
      case "result": {
        if (e.call.name === "ask_user") return; // the tool's "waiting" acknowledgement
        const tool = this.calls.get(e.call.id);
        if (tool) {
          tool.done = true;
          tool.result = e.text;
          tool.error = e.error;
        } else {
          this.current(e.by).tools.push({ name: e.call.name, done: true, result: e.text, error: e.error });
        }
        return;
      }
      case "reply": {
        const msg = this.current(e.by);
        msg.turn = e.turn;
        msg.notice = undefined;
        msg.interrupted = false;
        msg.text = e.text;
        msg.streaming = false;
        this.lastTurn = { id: e.turn, ended: true };
        return;
      }
      case "policy":
        return; // the reply as shipped already carries what a policy added; the Trace shows the rest
    }
  }

  /** Mark the last agent message resumable if its turn stopped part-way. */
  markInterrupted(): void {
    if (this.lastTurn.ended || !this.lastTurn.id) return;
    const last = this.messages[this.messages.length - 1];
    if (last?.question) return; // waiting on the user, not stopped
    if (last && last.role === "agent") {
      last.interrupted = true;
      last.turn = this.lastTurn.id;
    }
  }

  finish(): void {
    for (const m of this.messages) {
      m.streaming = false;
      for (const t of m.tools) t.done = true; // a finished run has no live calls
    }
  }
}

export function foldEvents(events: ConsoleEvent[]): ChatMessage[] {
  const folder = new ConversationFolder();
  for (const e of events) folder.push(e);
  folder.finish();
  folder.markInterrupted();
  return folder.messages;
}
