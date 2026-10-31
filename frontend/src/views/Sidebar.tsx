import { relTime } from "../lib/format";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { sessionTitle } from "../lib/titles";
import type { SessionRow } from "../lib/types";
import { IconInbox, IconPencil, IconPlus, IconSearch, IconTrash, IconX } from "../ui/Icons";

/**
 * Human name for a session owner. The user id is storage plumbing — the
 * console writes as "webui", and the MCP server files a caller's conversation
 * under "mcp" or "mcp:<caller>" — so it is translated rather than shown raw.
 * An owner we do not recognise is shown verbatim, which beats mislabelling it.
 */
export function sourceLabel(userId: string): string {
  if (userId === "webui") return "Chat";
  if (userId === "mcp") return "MCP";
  if (userId === "inbox") return "Inbox";
  if (userId.startsWith("mcp:")) return `MCP (${userId.slice(4)})`;
  return userId;
}

/** Coarse recency buckets, in display order. */
function bucketOf(epochSec: number): string {
  const days = (Date.now() / 1000 - epochSec) / 86400;
  if (days < 1) return "Today";
  if (days < 2) return "Yesterday";
  if (days < 7) return "Previous 7 days";
  if (days < 30) return "Previous 30 days";
  return "Older";
}
const BUCKET_ORDER = ["Today", "Yesterday", "Previous 7 days", "Previous 30 days", "Older"];

interface RowProps {
  session: SessionRow;
  /** Passed in rather than read here: titles live outside React, and the row
   *  is memoized, so a rename has to reach it as a changed prop. */
  title: string;
  active: boolean;
  canDelete: boolean;
  // Stable callbacks taking the row's own ids, so the memo holds.
  onSelect: (id: string, owner: string) => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
}

const SessionRow = memo(function SessionRow({ session, title, active, canDelete, onSelect, onRename, onDelete }: RowProps) {
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  function startRename() {
    setDraft(title);
    setEditing(true);
  }
  function commit() {
    onRename(session.id, draft);
    setEditing(false);
  }

  if (editing) {
    return (
      <div className="session-item editing">
        <input
          ref={inputRef}
          className="rename-input"
          aria-label="Session name"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") setEditing(false);
          }}
          onBlur={commit}
        />
      </div>
    );
  }

  if (confirming) {
    return (
      <div className="session-item confirming">
        <span className="title">Delete this session?</span>
        <button type="button" className="confirm-yes" onClick={() => onDelete(session.id)}>
          Delete
        </button>
        <button type="button" className="confirm-no" onClick={() => setConfirming(false)}>
          Cancel
        </button>
      </div>
    );
  }

  // The row is a container, not a control: rename and delete are real buttons
  // and cannot be nested inside the button that opens the session — nesting
  // them costs keyboard access to both.
  return (
    <div className={"session-item" + (active ? " active" : "")}>
      <button type="button" className="session-open" aria-current={active} onClick={() => onSelect(session.id, session.user)}>
        {session.user === "inbox" && <span className="row-source" role="img" aria-label="From the Inbox" title="From the Inbox"><IconInbox /></span>}
        {session.running && <span className="row-running" role="img" aria-label="Running a turn" title="Running a turn" />}
        <span className="title" title={title}>{title}</span>
        <time dateTime={new Date(session.updated * 1000).toISOString()}>
          {relTime(session.updated)}
        </time>
      </button>
      <span className="row-actions">
        <button type="button" className="row-btn" title="Rename" aria-label={`Rename “${title}”`} onClick={startRename}>
          <IconPencil />
        </button>
        {canDelete && (
          <button
            type="button"
            className="row-btn danger"
            title="Delete"
            aria-label={`Delete “${title}”`}
            onClick={() => setConfirming(true)}
          >
            <IconTrash />
          </button>
        )}
      </span>
    </div>
  );
});

export function Sidebar(props: {
  sessions: SessionRow[] | null;
  activeId: string | null;
  open: boolean;
  onSelect: (id: string, owner: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
}) {
  const { sessions, activeId, open } = props;
  const [query, setQuery] = useState("");

  const groups = useMemo(() => {
    if (!sessions) return null;
    const q = query.trim().toLowerCase();
    const filtered = q
      ? sessions.filter((s) => sessionTitle(s).toLowerCase().includes(q))
      : sessions;
    const byBucket = new Map<string, SessionRow[]>();
    for (const s of filtered) {
      const b = bucketOf(s.updated);
      (byBucket.get(b) ?? byBucket.set(b, []).get(b)!).push(s);
    }
    return BUCKET_ORDER.filter((b) => byBucket.has(b)).map((b) => [b, byBucket.get(b)!] as const);
  }, [sessions, query]);

  return (
    <nav className={"rail" + (open ? " open" : "")} aria-label="Sessions">
      {/* Turns no longer block the console (a running one streams on its own),
          so starting a new session is always allowed. */}
      <button type="button" className="new-chat" onClick={props.onNew}>
        <IconPlus />
        New session
      </button>

      {sessions && sessions.length > 3 && (
        <div className="rail-search">
          <IconSearch />
          <input
            type="search"
            aria-label="Search sessions"
            placeholder="Search sessions"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <button type="button" className="row-btn" onClick={() => setQuery("")} title="Clear search" aria-label="Clear search">
              <IconX />
            </button>
          )}
        </div>
      )}

      <div className="session-list">
        {sessions === null ? (
          <>
            <div className="skeleton" />
            <div className="skeleton" />
            <div className="skeleton" />
          </>
        ) : sessions.length === 0 ? (
          <div className="rail-empty">
            No sessions yet — start one above.
          </div>
        ) : groups && groups.length === 0 ? (
          <div className="rail-empty">No sessions match “{query}”.</div>
        ) : (
          groups!.map(([bucket, items]) => (
            <div key={bucket}>
              <div className="rail-label">{bucket}</div>
              {items.map((s) => (
                <SessionRow
                  key={s.id}
                  session={s}
                  title={sessionTitle(s)}
                  active={s.id === activeId}
                  canDelete={s.user !== "inbox"}  // the Inbox's are not yours to delete
                  onSelect={props.onSelect}
                  onRename={props.onRename}
                  onDelete={props.onDelete}
                />
              ))}
            </div>
          ))
        )}
      </div>
    </nav>
  );
}
