import { useMemo } from "react";
import { agentLabel, compact, shortModel } from "../lib/format";
import type { ConsoleEvent } from "../lib/types";
import { IconTrace } from "../ui/Icons";
import { character } from "../ui/character";

/** Stable lane color per author, from a small themed palette. */
const LANE_COLORS = ["#1f6fb2", "#1a7f4b", "#8a5a00", "#a03e99", "#0e7490", "#b3261e"];
function laneColor(author: string, lanes: Map<string, string>): string {
  if (!lanes.has(author)) {
    lanes.set(author, LANE_COLORS[lanes.size % LANE_COLORS.length]);
  }
  return lanes.get(author)!;
}

interface Row {
  event: ConsoleEvent;
  kind: string;
  detail: string;
  isError: boolean;
  model?: string; // a step's, on its first row
  tokens?: { input: number; output: number };
}

interface Invocation {
  id: string;
  question: string;
  start: number;
  end: number;
  rows: Row[];
  models: Set<string>; // distinct models that ran in this turn
  tokens: number; // total tokens billed across the turn
}

const cut = (text: string) => (text.length > 110 ? text.slice(0, 110) + "…" : text);

function summarize(e: ConsoleEvent): Row[] {
  switch (e.kind) {
    case "step": {
      const rows: Row[] = e.calls.map((c) => {
        const args = JSON.stringify(c.args ?? {});
        return { event: e, kind: "tool call", detail: `${c.name}(${cut(args)})`, isError: false };
      });
      if (e.text) rows.unshift({ event: e, kind: "text", detail: cut(e.text), isError: false });
      if (rows.length) Object.assign(rows[0], { model: e.model, tokens: e.tokens });
      return rows;
    }
    case "result":
      return [{ event: e, kind: e.held ? "held" : "tool result", detail: `${e.call.name} → ${cut(e.text)}`, isError: e.error }];
    case "reply":
      return [{ event: e, kind: "reply", detail: cut(e.text), isError: false }];
    case "policy":
      return [{ event: e, kind: e.to_model ? "policy → model" : "policy", detail: `${e.policy}: ${cut(e.said)}`, isError: false }];
    default:
      return [];
  }
}

function fold(events: ConsoleEvent[]): Invocation[] {
  const invocations: Invocation[] = [];
  let current: Invocation | null = null;

  const open = (e: ConsoleEvent, question: string): Invocation => {
    const inv: Invocation = {
      id: `${e.turn}-${invocations.length}`, question, start: e.at, end: e.at,
      rows: [], models: new Set(), tokens: 0,
    };
    invocations.push(inv);
    return inv;
  };

  for (const e of events) {
    // What the person said opens a turn; the rest is what answered it.
    if (e.kind === "said") {
      current = open(e, e.text.trim() || (e.files.length ? "(attachments)" : "(empty message)"));
      continue;
    }
    if (!current) current = open(e, "(session start)");
    current.rows.push(...summarize(e));
    if (e.at > current.end) current.end = e.at;
    // Roll up which models ran and what they cost — the header answers
    // "what did this turn use?" without reading every row.
    if (e.kind === "step") {
      if (e.model) current.models.add(shortModel(e.model));
      current.tokens += e.tokens ? e.tokens.input + e.tokens.output : 0;
    }
  }
  return invocations;
}

function clock(epochSec: number): string {
  return new Date(epochSec * 1000).toLocaleTimeString("en-GB", { hour12: false });
}

export function Trace({ events }: { events: ConsoleEvent[] }) {
  const invocations = useMemo(() => fold(events), [events]);
  const lanes = useMemo(() => new Map<string, string>(), [events]);
  const errors = invocations.reduce((n, inv) => n + inv.rows.filter((r) => r.isError).length, 0);

  if (invocations.length === 0) {
    return (
      <div className="empty">
        <span style={{ color: "var(--accent)" }}>
          <IconTrace />
        </span>
        <h2>Nothing to trace yet</h2>
        <p>
          Every agent turn in this session shows up here as a timeline — which
          sub-agent ran, each tool call with its result, and any errors.
        </p>
      </div>
    );
  }

  return (
    <div className="trace-scroll">
      <div className="trace">
        {character.Page && <character.Page page="trace" say={errors ? `${errors} error${errors > 1 ? "s" : ""}!` : "No errors!"} />}
        {invocations.map((inv) => (
            <section className="invocation" key={inv.id}>
              <div className="inv-head">
                <span className="q">{inv.question}</span>
                <time>
                  {clock(inv.start)} · {(inv.end - inv.start).toFixed(1)}s
                  {inv.models.size > 0 && ` · ${[...inv.models].join(", ")}`}
                  {inv.tokens > 0 && ` · ${compact(inv.tokens)} tok`}
                </time>
              </div>
              <table>
                <thead>
                  <tr>
                    <th scope="col">Time</th>
                    <th scope="col">Agent</th>
                    <th scope="col">Kind</th>
                    <th scope="col">Detail</th>
                    <th scope="col">Model</th>
                    <th scope="col" className="num">Tokens</th>
                  </tr>
                </thead>
                <tbody>
                  {inv.rows.map((row, i) => {
                    const ts = row.event.at || inv.start;
                    const author = row.event.by || "?";
                    const usage = row.tokens;
                    return (
                      <tr key={i}>
                        <td className="t">+{(ts - inv.start).toFixed(1)}s</td>
                        <td className="author">
                          <span className="chip">
                            {/* The lane colour keys off the raw id, not the
                                label: two ids that render alike must still get
                                their own colour. */}
                            <span className="dot" style={{ background: laneColor(author, lanes) }} />
                            {agentLabel(author)}
                          </span>
                        </td>
                        <td className="kind">{row.kind}</td>
                        <td className="detail">
                          {row.isError ? <span className="err">{row.detail}</span> : row.detail}
                        </td>
                        <td className="model">
                          {row.model ? shortModel(row.model) : ""}
                        </td>
                        <td className="num" title={usage ? `${usage.input} in / ${usage.output} out` : ""}>
                          {usage ? compact(usage.input + usage.output) : ""}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </section>
        ))}
      </div>
    </div>
  );
}
