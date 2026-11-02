import { useEffect, useState } from "react";
import { getWorkstream, listWorkstreams, INBOX_USER } from "../lib/api";
import { useNavigate } from "../lib/nav";
import type { Workstream, WorkstreamRow } from "../lib/types";
import { Md } from "../markdown/Md";

/** The pieces of work meeseek and Claude Code share one context on — a
 *  ticket, an MR, a branch (runtime/workstreams.py): what each is, where it
 *  stands, and the conversations and Claude sessions that worked on it. */

function ago(epoch: number): string {
  const s = Date.now() / 1000 - epoch;
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return new Date(epoch * 1000).toLocaleDateString();
}

export function Work({ id }: { id?: string }) {
  const navigate = useNavigate();
  const [filter, setFilter] = useState<"open" | "all">("open");
  const [rows, setRows] = useState<WorkstreamRow[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    const load = () =>
      listWorkstreams(filter === "all")
        .then((r) => { if (live) { setRows(r); setError(""); } })
        .catch((e) => live && setError(String(e)));
    void load();
    const timer = setInterval(() => { if (!document.hidden) void load(); }, 30_000);
    return () => { live = false; clearInterval(timer); };
  }, [filter]);

  return (
    <div className="usage-scroll">
      <div className="usage inbox">
        {error && <div className="notice">{error}</div>}
        <div className="seg inbox-filter" role="group" aria-label="Filter">
          <button aria-pressed={filter === "open"} onClick={() => setFilter("open")}>Open</button>
          <button aria-pressed={filter === "all"} onClick={() => setFilter("all")}>All</button>
        </div>
        {rows === null ? (
          <div className="skeleton" />
        ) : rows.length === 0 ? (
          <p className="usage-note">
            No work tracked yet. A workstream starts when a conversation names a ticket or an MR.
          </p>
        ) : (
          rows.map((w) => {
            const open = w.id === id;
            return (
              <div key={w.id} className={`learn-card inbox-item ${open ? "open" : ""}`}>
                <button className="inbox-item-head" aria-expanded={open}
                        onClick={() => navigate(open ? { view: "work" } : { view: "work", id: w.id })}>
                  <span className="chip">{w.key}</span>
                  <span className="inbox-title">{w.title}</span>
                  {w.status === "closed" && <span className="inbox-pill done">Closed</span>}
                  <span className="learn-date">{ago(w.updated)}</span>
                </button>
                <div className="inbox-sub">
                  {w.state || "No summary yet"}
                  {w.next && <span> · Next: {w.next}</span>}
                </div>
                {open && <Detail id={w.id} />}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function Detail({ id }: { id: string }) {
  const navigate = useNavigate();
  const [ws, setWs] = useState<Workstream | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    getWorkstream(id).then((w) => live && setWs(w)).catch((e) => live && setError(String(e)));
    return () => { live = false; };
  }, [id]);

  if (error) return <div className="notice">{error}</div>;
  if (!ws) return <div className="skeleton" />;
  return (
    <div className="inbox-detail">
      <Md>{ws.context}</Md>
      {ws.sessions.length > 0 && (
        <div className="inbox-sub">
          Conversations:{" "}
          {ws.sessions.map((s, i) => (
            <span key={s}>
              {i > 0 && ", "}
              <button type="button" className="btn-ghost small"
                      onClick={() => navigate({ view: "chat", session: s,
                                                owner: s.startsWith("inbox-") ? INBOX_USER : undefined })}>
                {s}
              </button>
            </span>
          ))}
        </div>
      )}
      {ws.closed && <div className="inbox-sub">Closed {ago(ws.closed.at)}: {ws.closed.why}</div>}
    </div>
  );
}
