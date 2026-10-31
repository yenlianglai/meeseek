import { useEffect, useRef, useState } from "react";
import { claudeApi } from "../lib/api";
import { relTime } from "../lib/format";
import type { ClaudeSessionRow } from "../lib/types";
import { IconClaude, IconX } from "../ui/Icons";

/* Which Claude Code session this conversation's tasks go to. meeseek does
   the handing over — the target only rides on each message as one line
   (`[Claude session: <id> · <title>]`, runtime/claude_agent.py:directive) that
   the orchestrator's prompt reads. The choice holds for the conversation
   until cleared, so follow-ups go to the same session. */

export interface ClaudeTarget {
  session_id: string;   // "new" = a new session
  title: string;
}

const KEY = (session: string) => `meeseek.claude-target.${session}`;

export function directive(t: ClaudeTarget): string {
  return t.session_id === "new" ? "[Claude session: new]" : `[Claude session: ${t.session_id} · ${t.title}]`;
}

const DIRECTIVE = /^\[Claude session: (new|[0-9a-f-]{8,})(?: · ([^\]\n]*))?\]\n?/;

/** A message's directive line, split off for display. */
export function splitDirective(text: string): { target: ClaudeTarget | null; rest: string } {
  const m = DIRECTIVE.exec(text);
  if (!m) return { target: null, rest: text };
  return { target: { session_id: m[1], title: m[2] ?? (m[1] === "new" ? "New Claude session" : m[1].slice(0, 8)) },
           rest: text.slice(m[0].length) };
}

/** The target of one conversation, remembered across reloads. */
export function useClaudeTarget(session: string | null): [ClaudeTarget | null, (t: ClaudeTarget | null) => void] {
  const [target, setTarget] = useState<ClaudeTarget | null>(null);
  useEffect(() => {
    if (!session) return setTarget(null);
    try {
      const raw = localStorage.getItem(KEY(session));
      setTarget(raw ? (JSON.parse(raw) as ClaudeTarget) : null);
    } catch {
      setTarget(null);
    }
  }, [session]);
  function set(t: ClaudeTarget | null) {
    setTarget(t);
    if (!session) return;
    try {
      if (t) localStorage.setItem(KEY(session), JSON.stringify(t));
      else localStorage.removeItem(KEY(session));
    } catch {
      /* storage off: the choice lasts this page */
    }
  }
  return [target, set];
}

const home = (p: string) => p.replace(/^\/Users\/[^/]+/, "~");

/** The composer's button: pick the Claude session the next tasks go to. */
export function ClaudeTargetPicker({ target, onPick, disabled }: {
  target: ClaudeTarget | null;
  onPick: (t: ClaudeTarget | null) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<ClaudeSessionRow[] | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => {
      claudeApi<ClaudeSessionRow[]>(`/sessions?limit=30&query=${encodeURIComponent(query)}`)
        .then((r) => { setRows(r); setError(""); })
        .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    }, 150);
    return () => clearTimeout(t);
  }, [open, query]);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);

  function pick(t: ClaudeTarget | null) {
    onPick(t);
    setOpen(false);
  }

  return (
    <div className="claude-picker" ref={box}>
      <button type="button" className={"attach" + (target ? " on" : "")} disabled={disabled}
              onClick={() => setOpen((o) => !o)} aria-expanded={open}
              aria-label="Choose the Claude session these tasks go to"
              title={target ? `Tasks go to Claude: ${target.title}` : "Hand these tasks to a Claude Code session"}>
        <IconClaude />
      </button>
      {open && (
        <div className="claude-menu" role="dialog" aria-label="Claude sessions">
          <input className="claude-menu-search" type="search" autoFocus placeholder="Search Claude sessions"
                 value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search Claude sessions" />
          <div className="claude-menu-list">
            <button type="button" className="claude-menu-item" onClick={() => pick({ session_id: "new", title: "New Claude session" })}>
              <span className="claude-menu-title">+ New Claude session</span>
              <span className="claude-menu-meta">meeseek picks the folder</span>
            </button>
            {target && (
              <button type="button" className="claude-menu-item" onClick={() => pick(null)}>
                <span className="claude-menu-title">No Claude session</span>
                <span className="claude-menu-meta">let meeseek decide who does the work</span>
              </button>
            )}
            {error && <div className="claude-menu-meta">{error}</div>}
            {rows === null && !error && <div className="skeleton" />}
            {rows?.map((r) => (
              <button key={r.session_id} type="button"
                      className={"claude-menu-item" + (target?.session_id === r.session_id ? " sel" : "")}
                      onClick={() => pick({ session_id: r.session_id, title: r.title || r.session_id.slice(0, 8) })}>
                <span className="claude-menu-title">
                  {r.desktop && <span className="claude-menu-badge" title="Opened in Claude Desktop — what meeseek adds shows there when you reopen it">Desktop</span>}
                  {r.title || r.first_prompt || r.session_id}
                </span>
                <span className="claude-menu-meta">
                  {home(r.cwd)}{r.branch ? ` · ${r.branch}` : ""} · {relTime(r.updated_at)}
                  {r.by_meeseek ? " · via meeseek" : ""}{r.archived ? " · archived" : ""}{r.status === "running" ? " · working" : ""}
                </span>
              </button>
            ))}
            {rows && rows.length === 0 && <div className="claude-menu-meta">No Claude sessions{query && ` matching “${query}”`}.</div>}
          </div>
        </div>
      )}
    </div>
  );
}

/** Above the composer while a target is set. */
export function ClaudeTargetChip({ target, onClear }: { target: ClaudeTarget; onClear?: () => void }) {
  return (
    <span className="claude-chip" title={target.session_id === "new" ? "" : target.session_id}>
      <IconClaude /> Claude · {target.title}
      {onClear && (
        <button type="button" onClick={onClear} aria-label="Stop handing tasks to this Claude session" title="Clear">
          <IconX />
        </button>
      )}
    </span>
  );
}
