import { useCallback, useEffect, useRef, useState } from "react";
import { inboxApi, INBOX_USER } from "../lib/api";
import { copyText } from "../lib/clipboard";
import { useNavigate } from "../lib/nav";
import type { InboxAction, InboxItem, InboxSourceStatus } from "../lib/types";
import { CopyButton } from "../ui/Copy";
import { Md } from "../markdown/Md";
import { character } from "../ui/character";

/* Events that started work on their own — mail, chat, alerts, meetings — and
   the one decision that is yours: a reply is a draft you copy and send, and
   every write the agent wants waits here for your yes. The feed is polled once
   for the whole console, so the tab's badge and the desktop notification come
   from the same read as the page. */

const POLL_MS = 20_000;

const SOURCE: Record<InboxItem["source"], string> = {
  gmail: "Gmail", chat: "Chat", alert: "Alert", calendar: "Calendar", meeting: "Meeting",
  telegram: "Telegram", gitlab: "GitLab", jira: "Jira",
};

const STATUS: Record<string, string> = {
  new: "Triaging", working: "Working…", recording: "Recording", paused: "Paused", recorded: "Recorded", ready: "Ready", needs_approval: "Needs approval",
  needs_input: "Needs answer", done: "Done", dismissed: "Dismissed", ignored: "Ignored",
  ignore: "Ignored", fyi: "FYI", failed: "Failed", expired: "Expired",
};

const WAITING = new Set(["ready", "needs_approval", "needs_input"]);
const SETTLED = new Set(["dismissed", "ignored", "ignore", "fyi", "done", "expired"]);

export interface InboxFeed {
  items: InboxItem[] | null;
  sources: Record<string, InboxSourceStatus>;
  enabled: string[];
  waiting: number; // unseen items that wait on you
  refresh: () => Promise<void>;
}

function notify(item: InboxItem) {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  const n = new Notification(`${SOURCE[item.source]}：${item.title}`, {
    body: item.triage.summary || STATUS[item.status],
    tag: item.id,
  });
  n.onclick = () => {
    window.focus();
    window.location.hash = `#/inbox/${item.id}`;
  };
}

/** The console-wide poll of the Inbox: items, source health, and a desktop
 *  notification the first time an item comes to wait on you. */
export function useInboxFeed(): InboxFeed {
  const [items, setItems] = useState<InboxItem[] | null>(null);
  const [sources, setSources] = useState<Record<string, InboxSourceStatus>>({});
  const [enabled, setEnabled] = useState<string[]>([]);
  const told = useRef<Set<string> | null>(null);
  // What the last poll stored, as sent: an unchanged poll sets no state, so it
  // does not re-render the whole app every 20 s for nothing.
  const last = useRef({ items: "", status: "" });

  const refresh = useCallback(async () => {
    const [list, status] = await Promise.all([
      inboxApi<InboxItem[]>("/items").catch(() => null),
      inboxApi<{ enabled: string[]; sources: Record<string, InboxSourceStatus> }>("/status").catch(() => null),
    ]);
    if (status) {
      const sig = JSON.stringify(status);
      if (sig !== last.current.status) {
        last.current.status = sig;
        setSources(status.sources);
        setEnabled(status.enabled);
      }
    }
    if (!list) return;
    const waiting = list.filter((i) => WAITING.has(i.status) && !i.seen);
    // The first read only learns what is already there: a reload is not news.
    if (told.current === null) {
      told.current = new Set(waiting.map((i) => `${i.id}:${i.status}`));
    } else {
      for (const i of waiting) {
        const k = `${i.id}:${i.status}`;
        if (!told.current.has(k)) {
          told.current.add(k);
          notify(i);
        }
      }
    }
    const sig = JSON.stringify(list);
    if (sig === last.current.items) return;
    last.current.items = sig;
    setItems(list);
  }, []);

  // Not while the tab is in the background; coming back to it catches up at once.
  useEffect(() => {
    refresh();
    const t = window.setInterval(() => { if (!document.hidden) void refresh(); }, POLL_MS);
    const onShow = () => { if (!document.hidden) void refresh(); };
    document.addEventListener("visibilitychange", onShow);
    return () => {
      window.clearInterval(t);
      document.removeEventListener("visibilitychange", onShow);
    };
  }, [refresh]);

  const waiting = (items ?? []).filter((i) => WAITING.has(i.status) && !i.seen).length;
  return { items, sources, enabled, waiting, refresh };
}

function ago(epoch: number): string {
  const s = Date.now() / 1000 - epoch;
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return new Date(epoch * 1000).toLocaleDateString();
}

/** The agent's reply without the draft block, which is shown on its own. */
function withoutDraft(reply: string): string {
  return reply.replace(/```reply[^\n]*\n[\s\S]*?```/g, "").trim();
}

function ActionCard({ item, action, onDone }: { item: InboxItem; action: InboxAction; onDone: (msg: string) => void }) {
  const [args, setArgs] = useState(JSON.stringify(action.args, null, 2));
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = action.status === "pending";

  async function verdict(v: "approve" | "reject") {
    let body: Record<string, unknown> = {};
    if (v === "approve") {
      try {
        body = { args: JSON.parse(args) };
      } catch {
        onDone("Arguments are not valid JSON");
        return;
      }
    } else {
      body = { note };
    }
    setBusy(true);
    try {
      await inboxApi(`/items/${item.id}/actions/${action.id}/${v}`, "POST", body);
      onDone(v === "approve" ? `Approved ${action.tool}; the agent is running it` : `Rejected ${action.tool}`);
    } catch (e) {
      onDone(`Failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`inbox-action ${action.status}`}>
      <div className="learn-head">
        <code>{action.tool}</code>
        <span className="learn-date">{action.agent}</span>
        <span className={`inbox-pill ${action.status}`}>
          {{ pending: "Needs approval", approved: "Approved", running: "Running", done: "Done",
             failed: "Failed", rejected: "Rejected", expired: "Expired" }[action.status]}
        </span>
      </div>
      {pending ? (
        <>
          <textarea className="inbox-args" value={args} spellCheck={false}
                    rows={Math.min(14, args.split("\n").length + 1)} onChange={(e) => setArgs(e.target.value)} />
          <input className="inbox-note" placeholder="Reason, if rejecting (the agent sees it, and meeseek learns from it)" value={note}
                 onChange={(e) => setNote(e.target.value)} />
          <div className="learn-actions">
            <button className="btn-primary" disabled={busy} onClick={() => verdict("approve")}>Approve &amp; run</button>
            <button className="btn-ghost danger" disabled={busy} onClick={() => verdict("reject")}>Reject</button>
          </div>
        </>
      ) : (
        <>
          <pre className="inbox-args-view">{JSON.stringify(action.args, null, 2)}</pre>
          {action.result && <div className="learn-reason">{action.result}</div>}
        </>
      )}
    </div>
  );
}

function ItemDetail({ item, refresh, say }: { item: InboxItem; refresh: () => Promise<void>; say: (m: string) => void }) {
  const navigate = useNavigate();
  const body = withoutDraft(item.reply);

  const [dismissing, setDismissing] = useState(false);
  async function post(path: string, msg: string, body?: unknown) {
    try {
      await inboxApi(`/items/${item.id}${path}`, "POST", body);
      say(msg);
    } catch (e) {
      say(`Failed: ${e instanceof Error ? e.message : e}`);
    }
    await refresh();
  }

  return (
    <div className="inbox-detail">
      {item.triage.why && <div className="learn-reason">{item.triage.why}</div>}
      {item.body && (
        <details className="learn-case">
          <summary>Original</summary>
          <div className="inbox-body">{item.body}</div>
        </details>
      )}
      {item.status === "working" && <div className="learn-reason">Working…</div>}
      {item.status === "recording" && <div className="learn-reason">Recording. Close the Meet tab to stop.</div>}
      {item.status === "paused" && (
        <div className="learn-reason">Paused: the Meet tab closed. Reopen it within 5 minutes and the recording goes on here.</div>
      )}
      {item.status === "recording" && item.meta.call_error ? (
        // Whether the tab is still in the call (runtime/meeting_recorder.py:in_call).
        <div className="learn-check not_followed">Running over: {String(item.meta.call_error)}</div>
      ) : null}
      {item.source === "meeting" && item.meta.coach_error ? (
        <div className="learn-check not_followed">Coach report: {String(item.meta.coach_error)}</div>
      ) : null}
      {typeof item.meta.audio === "string" && (
        <div className="learn-reason">Audio <code>{item.meta.audio}</code>
          {typeof item.meta.notes === "string" && <> · Notes <code>{item.meta.notes}</code></>}
        </div>
      )}
      {item.error && <div className="learn-check not_followed">{item.error}</div>}
      {body && <div className="md inbox-reply"><Md>{body}</Md></div>}
      {item.draft && (
        <div className="inbox-draft">
          <div className="learn-head">
            <strong>Reply draft</strong>
            <span className="inbox-draft-actions">
              <CopyButton title="Copy the draft" onCopy={() => copyText(item.draft)} />
              {item.link && <a className="tool-link" href={item.link} target="_blank" rel="noreferrer">Open thread ↗</a>}
            </span>
          </div>
          <div className="inbox-draft-text">{item.draft}</div>
        </div>
      )}
      {item.question && (
        <div className="inbox-question">
          <strong>Question:</strong> {item.question}
        </div>
      )}
      {item.actions.map((a) => (
        <ActionCard key={a.id} item={item} action={a} onDone={(m) => { say(m); refresh(); }} />
      ))}
      <div className="learn-actions">
        {item.session_id && (
          <button className="btn-ghost" onClick={() => navigate({ view: "chat", session: item.session_id, owner: INBOX_USER })}>
            {item.question ? "Answer in chat →" : "Open chat →"}
          </button>
        )}
        {!item.draft && item.link && (
          <a className="btn-ghost" href={item.link} target="_blank" rel="noreferrer">Open source ↗</a>
        )}
        {(item.status === "failed" || item.status === "fyi") && (
          <button className="btn-ghost" onClick={() => post("/run", "The agent is on it")}>
            {item.status === "failed" ? "Retry" : "Hand to agent"}
          </button>
        )}
        {!SETTLED.has(item.status) && !dismissing && (
          <button className="btn-ghost" onClick={() => setDismissing(true)}>Dismiss</button>
        )}
        {!SETTLED.has(item.status) && dismissing && (
          // Why, in one click: a misfiled item teaches the triage (runtime/feedback.py).
          <span className="dismiss-reasons" role="group" aria-label="Why dismiss">
            {["不相關", "分類錯", "已處理"].map((r) => (
              <button key={r} className="btn-ghost small" onClick={() => post("/dismiss", "Dismissed", { reason: r })}>{r}</button>
            ))}
            <button className="btn-ghost small" onClick={() => post("/dismiss", "Dismissed")}>Skip</button>
          </span>
        )}
      </div>
    </div>
  );
}

export function Inbox({ itemId, feed }: { itemId?: string; feed: InboxFeed }) {
  const navigate = useNavigate();
  const [filter, setFilter] = useState<"open" | "all">("open");
  const [note, setNote] = useState("");
  const [polling, setPolling] = useState(false);
  const [perm, setPerm] = useState(typeof Notification === "undefined" ? "unsupported" : Notification.permission);
  const { items, sources, enabled, refresh } = feed;

  useEffect(() => {
    if (!itemId) return;
    const item = items?.find((i) => i.id === itemId);
    if (item && !item.seen) inboxApi(`/items/${itemId}/seen`, "POST").then(refresh).catch(() => {});
  }, [itemId, items, refresh]);

  async function pollNow() {
    setPolling(true);
    try {
      const out = await inboxApi<{ filed: string[] }>("/poll", "POST");
      setNote(out.filed.length ? `${out.filed.length} new` : "Nothing new");
    } catch (e) {
      setNote(`Failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setPolling(false);
      refresh();
    }
  }

  const shown = (items ?? []).filter((i) => filter === "all" || !SETTLED.has(i.status));
  const waiting = (items ?? []).filter((i) => !SETTLED.has(i.status)).length;

  return (
    <div className="usage-scroll">
      <div className="usage inbox">
        {character.Page && <character.Page page="inbox" say={waiting ? `${waiting} waiting!` : "All clear!"} />}
        {note && <div className="notice">{note}</div>}
        <div className="inbox-sources">
          {enabled.length === 0 ? (
            <span className="usage-note">No sources on. Turn them on under <code>triggers:</code> in project.yaml.</span>
          ) : (
            enabled.map((name) => {
              const s = sources[name];
              return (
                <span key={name} className={`inbox-source ${s ? (!s.ok ? "bad" : s.error ? "warn" : "ok") : ""}`}
                      title={s?.error || (s ? `Checked ${ago(s.at)}` : "Not checked yet")}>
                  {name === "recorder" ? "Recorder" : SOURCE[name === "alerts" ? "alert" : (name as InboxItem["source"])] ?? name}
                  {s?.error && " ⚠"}
                </span>
              );
            })
          )}
          <span className="inbox-spacer" />
          {perm === "default" && (
            <button className="btn-ghost small" onClick={() => Notification.requestPermission().then(setPerm)}>
              Enable notifications
            </button>
          )}
          <button className="btn-ghost small" disabled={polling} onClick={pollNow}>Check now</button>
        </div>
        {/* A failing source, or one working around something (the recorder on the calendar alone). */}
        {Object.entries(sources).filter(([, s]) => s.error).map(([name, s]) => (
          <div key={name} className="learn-reason inbox-source-error">{name}：{s.error}</div>
        ))}

        <div className="seg inbox-filter" role="group" aria-label="Filter">
          <button aria-pressed={filter === "open"} onClick={() => setFilter("open")}>Open</button>
          <button aria-pressed={filter === "all"} onClick={() => setFilter("all")}>All</button>
        </div>

        {items === null ? (
          <div className="skeleton" />
        ) : shown.length === 0 ? (
          <p className="usage-note">Nothing waiting.</p>
        ) : (
          shown.map((i) => {
            const open = i.id === itemId;
            return (
              <div key={i.id} className={`learn-card inbox-item ${open ? "open" : ""} ${i.seen ? "" : "unseen"}`}>
                <button className="inbox-item-head" aria-expanded={open}
                        onClick={() => navigate(open ? { view: "inbox" } : { view: "inbox", item: i.id })}>
                  <span className={`chip src-${i.source}`}>{SOURCE[i.source]}</span>
                  <span className="inbox-title">{i.title}</span>
                  <span className={`inbox-pill ${i.status}`}>{STATUS[i.status] ?? i.status}</span>
                  <span className="learn-date">{ago(i.received_at)}</span>
                </button>
                <div className="inbox-sub">
                  {i.sender && <span>{i.sender}</span>}
                  {i.triage.summary && <span> · {i.triage.summary}</span>}
                </div>
                {open && <ItemDetail item={i} refresh={refresh} say={setNote} />}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
