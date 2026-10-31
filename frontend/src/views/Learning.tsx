import { useCallback, useEffect, useState } from "react";
import { learningApi } from "../lib/api";
import { useNavigate } from "../lib/nav";
import type { LearnedFact, LearnedRule, Review, ReviewAction, RuleVerdict } from "../lib/types";

/* The two entry-shaped kinds of context: learned rules (with the weekly review
   that keeps them few) and remembered facts. Both are written one entry at a
   time, through the same checks an agent's write goes through — a rule must
   pass the quality gate and not repeat another; a fact must not repeat one. */

const VERDICT: Record<RuleVerdict, string> = {
  working: "Working: followed only with the rule",
  redundant: "Redundant: followed without it too",
  not_followed: "Not followed, even with it",
  unknown: "Inconclusive",
  no_case: "No source conversation to replay",
};

const SHORT: Record<RuleVerdict, string> = {
  working: "working", redundant: "redundant", not_followed: "ignored", unknown: "?", no_case: "no case",
};

const ACTION: Record<ReviewAction["action"], string> = {
  remove: "Removed",
  graduate: "Ready for SKILL.md",
  stale: "Can't verify — your call",
};

const when = (iso?: string) => (iso ? iso.slice(0, 16).replace("T", " ") : "");

/** Run an action, show what it returned, refresh. */
function useActions(refresh: () => Promise<void>) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  async function act(key: string, run: () => Promise<string>): Promise<boolean> {
    setBusy(key);
    try {
      setNote(await run());
      await refresh();
      return true;
    } catch (e) {
      setNote(`Failed: ${e instanceof Error ? e.message : e}`);
      return false;
    } finally {
      setBusy("");
    }
  }
  return { note, busy, act };
}

function RuleEditor({ skills, initial, onSave, onCancel, busy }: {
  skills?: string[];
  initial?: { skill: string; text: string; reason: string };
  onSave: (v: { skill: string; text: string; reason: string }) => void;
  onCancel: () => void;
  busy: boolean;
}) {
  const [skill, setSkill] = useState(initial?.skill ?? skills?.[0] ?? "");
  const [text, setText] = useState(initial?.text ?? "");
  const [reason, setReason] = useState(initial?.reason ?? "");
  return (
    <div className="ctx-form">
      {skills && (
        <select value={skill} onChange={(e) => setSkill(e.target.value)} aria-label="Skill">
          {skills.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      )}
      <textarea rows={3} value={text} placeholder="One instruction: who does what, when (15–300 chars)"
                onChange={(e) => setText(e.target.value)} />
      <input value={reason} placeholder="Why (optional)" onChange={(e) => setReason(e.target.value)} />
      <div className="learn-actions">
        <button className="btn-primary" disabled={busy || !text.trim() || !skill}
                onClick={() => onSave({ skill, text, reason })}>Save</button>
        <button className="btn-ghost" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

type ScoreRow = {
  skill: string; turns: number; labeled_share: number; reward: number | null;
  approval_rate: number | null; correction_rate: number; reask_rate: number; failure_rate: number;
  infra_failure_rate: number; thumbs: [number, number]; cost_per_turn: number; latency_p50: number;
};

const pct = (x: number | null) => (x === null ? "—" : `${Math.round(x * 100)}%`);

/** How each skill is doing over four weeks (runtime/reward.py): what the weekly
 *  improvement run reads to choose what to work on. */
function Scoreboard() {
  const [data, setData] = useState<{ rows: ScoreRow[]; judge_trusted: boolean } | null>(null);
  useEffect(() => {
    learningApi<{ rows: ScoreRow[]; judge_trusted: boolean }>("/scoreboard?days=28")
      .then(setData).catch(() => setData({ rows: [], judge_trusted: true }));
  }, []);
  return (
    <section className="usage-section">
      <h2>How each skill is doing</h2>
      <p className="usage-note">
        Last 4 weeks. Reward is from your 👍/👎, Inbox decisions and corrections first, then a model's score where you
        said nothing{data && !data.judge_trusted && " (the model's score is off: it disagreed with you too often)"}.
      </p>
      {!data ? <div className="skeleton" /> : data.rows.length === 0 ? (
        <p className="usage-note">No turns recorded yet.</p>
      ) : (
        <table className="usage-table">
          <thead>
            <tr>
              <th scope="col">Skill</th>
              <th scope="col" className="num">Turns</th>
              <th scope="col" className="num">Reward</th>
              <th scope="col" className="num">👍 / 👎</th>
              <th scope="col" className="num">Writes approved</th>
              <th scope="col" className="num">Corrected</th>
              <th scope="col" className="num">Failed</th>
              <th scope="col" className="num">Labelled</th>
              <th scope="col" className="num">Cost / turn</th>
              <th scope="col" className="num">Time (p50)</th>
            </tr>
          </thead>
          <tbody>
            {data.rows.map((r) => (
              <tr key={r.skill}>
                <th scope="row">{r.skill}</th>
                <td className="num">{r.turns}</td>
                <td className="num">{r.reward === null ? "—" : r.reward.toFixed(2)}</td>
                <td className="num">{r.thumbs[0]} / {r.thumbs[1]}</td>
                <td className="num">{pct(r.approval_rate)}</td>
                <td className="num">{pct(r.correction_rate + r.reask_rate)}</td>
                <td className="num">{pct(r.failure_rate)}</td>
                <td className="num">{pct(r.labeled_share)}</td>
                <td className="num">${r.cost_per_turn.toFixed(3)}</td>
                <td className="num">{Math.round(r.latency_p50)}s</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

type Proposal = {
  id: string; kind: "edit" | "rollback"; skill: string; summary: string; status: string; created: number;
  diff: string; outcomes: Record<string, string>; cost_usd: number; note: string; commit: string;
  lesson: { cue: string; lesson: string; confidence: string } | null;
  cluster?: { size: number; requests: string[] };
};

/** What the weekly improvement run (runtime/improve.py) found to do better, waiting
 *  for your yes. Nothing changes until you approve; approving commits the edit and
 *  the agents take it on their next turn. */
function Proposals() {
  const [items, setItems] = useState<Proposal[] | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [said, setSaid] = useState("");
  const load = useCallback(() => {
    learningApi<Proposal[]>("/proposals").then(setItems).catch(() => setItems([]));
  }, []);
  useEffect(load, [load]);
  const decide = async (p: Proposal, verdict: "approve" | "reject") => {
    try {
      const out = await learningApi<{ status: string; commit?: string; why?: string }>(
        `/proposals/${p.id}/${verdict}`, "POST", { note: notes[p.id] ?? "" });
      setSaid(out.status === "approved" ? `Applied${out.commit ? ` (commit ${out.commit})` : ""}: live from the next turn`
        : out.status === "rejected" ? "Rejected; your note goes to the next run" : `Not applied: ${out.why ?? out.status}`);
    } catch (e) {
      setSaid(`Failed: ${e instanceof Error ? e.message : e}`);
    }
    load();
  };
  const pending = (items ?? []).filter((p) => p.status === "pending");
  const recent = (items ?? []).filter((p) => p.status !== "pending").slice(0, 5);
  return (
    <section className="usage-section">
      <h2>Proposed improvements</h2>
      <p className="usage-note">
        Each Monday meeseek replays the turns that went worst against an edited SOP or rule and proposes the edit
        only if it did better without breaking anything. Nothing changes until you approve.
      </p>
      {said && <div className="notice">{said}</div>}
      {!items ? <div className="skeleton" /> : pending.length === 0 ? (
        <p className="usage-note">Nothing waiting.</p>
      ) : pending.map((p) => {
        const res = Object.values(p.outcomes ?? {});
        return (
          <div key={p.id} className="learn-card">
            <div className="learn-head">
              <strong>{p.kind === "rollback" ? "Undo" : "Improve"} {p.skill}</strong>
              <span className="usage-note">{p.summary}</span>
            </div>
            {p.kind === "edit" && (
              <p className="usage-note">
                Replayed {res.length} cases: {res.filter((o) => o === "cand").length} better,{" "}
                {res.filter((o) => o === "base").length} worse, {res.filter((o) => o === "tie").length} the same ·
                cost US${p.cost_usd}
              </p>
            )}
            {p.lesson && <p className="usage-note">Lesson: when {p.lesson.cue} → {p.lesson.lesson}</p>}
            <details>
              <summary>The change</summary>
              <pre className="proposal-diff">{p.diff || "(no text change)"}</pre>
            </details>
            <div className="learn-actions">
              <button className="btn-ghost" onClick={() => decide(p, "approve")}>Approve</button>
              <input className="inbox-note" placeholder="Why not? (optional — the next run reads it)"
                     value={notes[p.id] ?? ""} onChange={(e) => setNotes({ ...notes, [p.id]: e.target.value })} />
              <button className="btn-ghost" onClick={() => decide(p, "reject")}>Reject</button>
            </div>
          </div>
        );
      })}
      {recent.length > 0 && (
        <p className="usage-note">
          Recent: {recent.map((p) => `${p.skill} ${p.status}${p.commit ? ` (${p.commit})` : ""}`).join(" · ")}
        </p>
      )}
    </section>
  );
}

export function RulesPanel() {
  const navigate = useNavigate();
  const [rules, setRules] = useState<LearnedRule[] | null>(null);
  const [review, setReview] = useState<Review | null>(null);
  const [skills, setSkills] = useState<string[]>([]);
  const [editing, setEditing] = useState<string>(""); // rule key, or "new"

  const refresh = useCallback(async () => {
    const [r, v] = await Promise.all([
      learningApi<LearnedRule[]>("/rules").catch(() => []),
      learningApi<Review>("/review").catch(() => ({ latest: null, running: false, archive: [] })),
    ]);
    setRules(r);
    setReview(v);
  }, []);
  const { note, busy, act } = useActions(refresh);

  useEffect(() => {
    refresh();
    learningApi<string[]>("/skills").then(setSkills).catch(() => {});
  }, [refresh]);

  const graduate = (skill: string, id: string) =>
    act(id, async () => {
      const out = await learningApi<{ commit: string }>(`/rules/${skill}/${id}/graduate`, "POST");
      return `Committed to skills/${skill}/SKILL.md as ${out.commit} (not pushed)`;
    });
  const forgetRule = (skill: string, id: string) =>
    act(id, async () => {
      await learningApi(`/rules/${skill}/${id}`, "DELETE");
      return "Rule deleted";
    });
  const restore = (skill: string, id: string) =>
    act(id, async () => {
      await learningApi(`/archive/${skill}/${id}/restore`, "POST");
      return "Restored; live from the next turn";
    });
  const runReview = () =>
    act("review", async () => {
      const out = await learningApi<{ started: boolean }>("/review/run", "POST");
      return out.started ? "Review started" : "A review is already running";
    });
  const add = (v: { skill: string; text: string; reason: string }) =>
    act("new", async () => {
      await learningApi("/rules", "POST", { skill: v.skill, text: v.text, reason: v.reason });
      return `Added to ${v.skill}; live from the next turn`;
    }).then((ok) => ok && setEditing(""));
  const edit = (r: LearnedRule, v: { text: string; reason: string }) =>
    act(r.id, async () => {
      await learningApi(`/rules/${r.skill}/${r.id}`, "PUT", { text: v.text, reason: v.reason });
      return "Rule updated; its review starts over";
    }).then((ok) => ok && setEditing(""));

  const latest = review?.latest;
  const live = new Set((rules ?? []).map((r) => `${r.skill}/${r.id}`));
  const archivedIds = new Set((review?.archive ?? []).map((a) => `${a.skill}/${a.id}`));
  const perSkill = (rules ?? []).reduce<Record<string, number>>(
    (n, r) => ({ ...n, [r.skill]: (n[r.skill] ?? 0) + 1 }), {});

  return (
    <>
      {note && <div className="notice">{note}</div>}

      <Proposals />

      <Scoreboard />

      <section className="usage-section">
        <h2>Weekly review</h2>
        <p className="usage-note">
          {latest?.finished ? <>Last run {when(latest.finished)} · {latest.rules.length} rules</> : "Mondays 02:00"}
          {review?.running && <> · running…</>}
        </p>
        <div className="learn-actions">
          <button className="btn-ghost" disabled={!!busy || review?.running} onClick={runReview}>Run now</button>
        </div>
        {latest === undefined ? (
          <div className="skeleton" />
        ) : !latest ? (
          <p className="usage-note">No reviews yet.</p>
        ) : latest.actions.length === 0 ? (
          <p className="usage-note">Nothing to act on.</p>
        ) : (
          latest.actions.map((a) => {
            const key = `${a.skill}/${a.id}`;
            return (
              <div key={key} className="learn-card">
                <div className="learn-head">
                  <span className="chip">{a.skill}</span>
                  <span className={`learn-action ${a.action}`}>{ACTION[a.action]}</span>
                </div>
                <div className="learn-text">{a.text}</div>
                {a.why && <div className="learn-reason">{a.why}</div>}
                <div className="learn-actions">
                  {a.action === "remove" && archivedIds.has(key) && (
                    <button className="btn-ghost" disabled={!!busy} onClick={() => restore(a.skill, a.id)}>Restore</button>
                  )}
                  {a.action === "graduate" && live.has(key) && (
                    <button className="btn-primary" disabled={!!busy} onClick={() => graduate(a.skill, a.id)}>Move to SKILL.md</button>
                  )}
                  {a.action !== "remove" && live.has(key) && (
                    <button className="btn-ghost danger" disabled={!!busy} onClick={() => forgetRule(a.skill, a.id)}>Delete</button>
                  )}
                </div>
              </div>
            );
          })
        )}
      </section>

      <section className="usage-section">
        <h2>Rules</h2>
        <p className="usage-note">
          Injected into every turn of their skill. Up to 10 per skill
          {Object.keys(perSkill).length > 0 && (
            <> · {Object.entries(perSkill).map(([s, n]) => `${s} ${n}/10`).join(" · ")}</>
          )}
        </p>
        {editing === "new" ? (
          <RuleEditor skills={skills} busy={!!busy} onSave={add} onCancel={() => setEditing("")} />
        ) : (
          <div className="learn-actions">
            <button className="btn-ghost" onClick={() => setEditing("new")}>New rule</button>
          </div>
        )}
        {rules === null ? (
          <div className="skeleton" />
        ) : rules.length === 0 ? (
          <p className="usage-note">No rules.</p>
        ) : (
          rules.map((r) => {
            const key = `${r.skill}/${r.id}`;
            return (
              <div key={key} className="learn-card">
                <div className="learn-head">
                  <span className="chip">{r.skill}</span>
                  <span className="learn-date">{r.date || "undated"} · {r.source}</span>
                  {r.verdicts.length > 0 && (
                    <span className="learn-verdicts" title="Review verdicts, oldest first">
                      {r.verdicts.slice(-4).map((v, i) => (
                        <span key={i} className={`learn-verdict ${v}`}>{SHORT[v]}</span>
                      ))}
                    </span>
                  )}
                </div>
                {editing === key ? (
                  <RuleEditor initial={{ skill: r.skill, text: r.text, reason: r.reason }} busy={!!busy}
                              onSave={(v) => edit(r, v)} onCancel={() => setEditing("")} />
                ) : (
                  <>
                    <div className="learn-text">{r.text}</div>
                    {r.reason && <div className="learn-reason">{r.reason}</div>}
                    {r.check && (
                      <div className={`learn-check ${r.check.verdict}`} title={
                        [r.check.with && `With: ${r.check.with.why}`, r.check.without && `Without: ${r.check.without.why}`]
                          .filter(Boolean).join("\n")}>
                        {VERDICT[r.check.verdict]}
                      </div>
                    )}
                    {r.case && r.case.turns.length > 0 && (
                      <details className="learn-case">
                        <summary>Source conversation ({r.case.turns.length})</summary>
                        <ol>{r.case.turns.map((t, i) => <li key={i}>{t}</li>)}</ol>
                        {r.case.session_id && (
                          <button className="tool-link" onClick={() => navigate({
                            view: "chat", session: r.case!.session_id, owner: r.case!.user_id || undefined })}>
                            Open conversation →
                          </button>
                        )}
                      </details>
                    )}
                    <div className="learn-actions">
                      <button className="btn-ghost" disabled={!!busy} onClick={() => setEditing(key)}>Edit</button>
                      <button className="btn-primary" disabled={!!busy} onClick={() => graduate(r.skill, r.id)}>Move to SKILL.md</button>
                      <button className="btn-ghost danger" disabled={!!busy}
                              onClick={() => confirm("Delete this rule?") && forgetRule(r.skill, r.id)}>Delete</button>
                    </div>
                  </>
                )}
              </div>
            );
          })
        )}
      </section>

      {review && review.archive.length > 0 && (
        <section className="usage-section">
          <h2>Archive</h2>
          <p className="usage-note">Rules the review removed.</p>
          <table className="usage-table">
            <thead><tr><th>Rule</th><th>Why</th><th>Removed</th><th /></tr></thead>
            <tbody>
              {review.archive.map((a) => (
                <tr key={`${a.skill}/${a.id}`}>
                  <td><span className="chip">{a.skill}</span> {a.text}</td>
                  <td>{a.why}</td>
                  <td className="num">{when(a.at).slice(0, 10)}</td>
                  <td className="learn-row-actions">
                    <button className="btn-ghost" disabled={!!busy} onClick={() => restore(a.skill, a.id)}>Restore</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}

export function FactsPanel() {
  const [facts, setFacts] = useState<LearnedFact[] | null>(null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<{ line: string; text: string } | null>(null);
  const refresh = useCallback(async () => {
    setFacts(await learningApi<LearnedFact[]>("/facts").catch(() => []));
  }, []);
  const { note, busy, act } = useActions(refresh);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const factAction = (f: LearnedFact, action: "pin" | "forget") =>
    act(f.line, async () => {
      await learningApi(`/facts/${action}`, "POST", { line: f.line });
      return action === "pin" ? "Pinned; it no longer expires" : "Fact deleted";
    });
  const add = () =>
    act("new", async () => {
      await learningApi("/facts/add", "POST", { text: draft });
      return "Remembered; every agent sees it from the next turn";
    }).then((ok) => ok && setDraft(""));
  const save = () =>
    editing && act(editing.line, async () => {
      await learningApi("/facts/edit", "POST", { line: editing.line, text: editing.text });
      return "Fact updated";
    }).then((ok) => ok && setEditing(null));

  return (
    <>
      {note && <div className="notice">{note}</div>}
      <section className="usage-section">
        <h2>Facts</h2>
        <p className="usage-note">
          The newest 20 reach every agent, each for 90 days. Pin one to keep it.
        </p>
        <div className="ctx-form inline">
          <input value={draft} placeholder="Add a fact"
                 onChange={(e) => setDraft(e.target.value)}
                 onKeyDown={(e) => e.key === "Enter" && draft.trim() && add()} />
          <button className="btn-primary" disabled={!!busy || !draft.trim()} onClick={add}>Add</button>
        </div>
        {facts === null ? (
          <div className="skeleton" />
        ) : facts.length === 0 ? (
          <p className="usage-note">No facts.</p>
        ) : (
          <table className="usage-table">
            <thead><tr><th>Fact</th><th>Added</th><th>Expires</th><th /></tr></thead>
            <tbody>
              {facts.map((f) => (
                <tr key={f.line} className={f.live ? "" : "expired"}>
                  <td>
                    {editing?.line === f.line ? (
                      <input className="ctx-cell-input" value={editing.text} autoFocus
                             onChange={(e) => setEditing({ line: f.line, text: e.target.value })}
                             onKeyDown={(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") setEditing(null); }} />
                    ) : f.text}
                  </td>
                  <td className="num">{f.date}</td>
                  <td className="num">{f.live ? f.expires : "not injected"}</td>
                  <td className="learn-row-actions">
                    {editing?.line === f.line ? (
                      <>
                        <button className="btn-primary" disabled={!!busy} onClick={save}>Save</button>
                        <button className="btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
                      </>
                    ) : (
                      <>
                        <button className="btn-ghost" disabled={!!busy} onClick={() => setEditing({ line: f.line, text: f.text })}>Edit</button>
                        <button className="btn-ghost" disabled={!!busy} onClick={() => factAction(f, "pin")}>Pin</button>
                        <button className="btn-ghost danger" disabled={!!busy}
                                onClick={() => confirm("Delete this fact?") && factAction(f, "forget")}>Delete</button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
