import type { Attachment, Session, SessionRow, StreamEvent } from "./types";

/**
 * The session owner the console writes as. Other owners exist — the MCP server
 * files a caller's conversation under "mcp" or "mcp:<caller>" — and the server
 * serves its session endpoints per user, so every read below takes an owner and only
 * defaults to this one. Writes stay on this owner: the console never posts
 * into somebody else's conversation.
 */
export const CONSOLE_USER = "webui";
/** Owner of the sessions the Inbox's triggers start; you can reply in them. */
export const INBOX_USER = "inbox";

/** A failed request. `status` lets a caller tell a conflict (409) from the rest;
 *  the message is the server's own `detail` when it sent one. */
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    const detail = await res.json().then((b) => b?.detail).catch(() => undefined);
    throw new ApiError(res.status, typeof detail === "string" ? detail : `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

/** Credential state. `ok: false` means the Workspace tools will 403. */
export type AuthHealth = {
  ok: boolean;
  reason?: string;
  fix?: string;
  missing?: string[];
  unverified?: string;
};

export function getAuthStatus(): Promise<{ status: string; auth?: AuthHealth }> {
  return request<{ status: string; auth?: AuthHealth }>("/auth_api/status");
}

const owner = (user: string) => `user=${encodeURIComponent(user)}`;

/** The user's conversations, newest first, each with its title. */
export function listSessions(user = CONSOLE_USER): Promise<SessionRow[]> {
  return request<SessionRow[]>(`/api/sessions?${owner(user)}`);
}

export function getSession(id: string, user = CONSOLE_USER): Promise<Session> {
  return request<Session>(`/api/sessions/${encodeURIComponent(id)}?${owner(user)}`);
}

export function createSession(user = CONSOLE_USER): Promise<SessionRow> {
  return request<SessionRow>("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user }),
  });
}

export function deleteSession(id: string, user = CONSOLE_USER): Promise<void> {
  return fetch(`/api/sessions/${encodeURIComponent(id)}?${owner(user)}`, { method: "DELETE" }).then((r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  });
}

/** The Learning page (`/learning_api/*`). */
export function learningApi<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  return request<T>(`/learning_api${path}`, {
    method,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
}

/** The Inbox (`/inbox_api/*`). */
export function inboxApi<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  return request<T>(`/inbox_api${path}`, {
    method,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
}

/** Context files (`/context_api/*`): pinned and other notes, meetings, outputs. */
export function contextApi<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  return request<T>(`/context_api${path}`, {
    method,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
}

/** Claude Code sessions (`/claude_api/*`). */
export function claudeApi<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  return request<T>(`/claude_api${path}`, {
    method,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
}

/** LLM usage accounting (`/usage_api/*`). */
export function usageApi<T>(path: string): Promise<T> {
  return request<T>(`/usage_api${path}`);
}

/**
 * A new turn: the message and its files (attachments lead, so the model reads
 * the prompt against files it has already seen), streamed back event by event.
 * Sending is possible in the console's own sessions and the Inbox's; sessions
 * owned by another user are browsed read-only.
 */
export function streamTurn(
  sessionId: string,
  text: string,
  attachments: Attachment[],
  signal: AbortSignal,
  user: string = CONSOLE_USER,
): AsyncGenerator<StreamEvent> {
  return sse(`/api/sessions/${encodeURIComponent(sessionId)}/turns`, signal, {
    user, text, files: attachments.map((a) => ({ mime: a.mimeType, data: a.data })),
  });
}

/** The turn that stopped (paused, or cut short), carried on from its last step. */
export function resumeTurn(sessionId: string, signal: AbortSignal, user: string = CONSOLE_USER): AsyncGenerator<StreamEvent> {
  return sse(`/api/sessions/${encodeURIComponent(sessionId)}/resume`, signal, { user });
}

/**
 * Watch a running turn: the events it has already streamed, then live to its
 * end. Yields nothing when the turn finished first (the server answers 204).
 * Closing it (the signal) only detaches — the turn runs on; stopTurn cancels.
 */
export function attachTurn(sessionId: string, signal: AbortSignal): AsyncGenerator<StreamEvent> {
  return sse(`/api/sessions/${encodeURIComponent(sessionId)}/stream`, signal);
}

/** Cancel the session's running turn; it pauses where it is, to be resumed. */
export function stopTurn(sessionId: string): Promise<{ stopped: boolean }> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/stop`, { method: "POST" });
}

/** Open an event stream (POST with a body; GET without) and yield each
 *  `data: {event}` line as it arrives; an error event ends it as a throw. */
async function* sse(url: string, signal: AbortSignal, body?: unknown): AsyncGenerator<StreamEvent> {
  const res = await fetch(url, {
    signal,
    ...(body === undefined
      ? { method: "GET" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (res.status === 204) return; // nothing running to watch
  if (!res.ok || !res.body) {
    const detail = await res.json().then((b) => b?.detail).catch(() => undefined);
    throw new Error(typeof detail === "string" ? detail : `${res.status} ${res.statusText}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        let event: StreamEvent;
        try {
          event = JSON.parse(line.slice(6));
        } catch {
          continue; // tolerate a malformed frame rather than killing the stream
        }
        if (event.kind === "error") throw new Error(event.message); // the server's last word on a failed turn
        yield event;
      }
    }
  }
}

/** Your 👍/👎 on a reply (runtime/feedback.py); `note` says what was wrong.
 *  It is what the weekly improvement run learns from most. */
export function rateTurn(turn: string, rating: 1 | -1, note = ""): Promise<{ ok: boolean }> {
  return request("/feedback_api/turn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ turn, rating, note }),
  });
}

/** A reply copied: a quiet sign it was useful. Fire and forget. */
export function noteCopy(turn: string, what: "answer" | "draft" = "answer"): void {
  fetch("/feedback_api/copy", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ turn, what }),
  }).catch(() => {});
}
