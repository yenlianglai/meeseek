import { useEffect, useState } from "react";
import { useNavigate } from "../lib/nav";
import { usageApi } from "../lib/api";
import { agentLabel, compact, shortModel } from "../lib/format";
import { IconChart } from "../ui/Icons";
import { character } from "../ui/character";

interface Bucket {
  requests: number;
  prompt: number;
  output: number;
  cached: number; // prompt tokens served from cache, at ~1/10 the price
  total: number;
  cost_usd: number; // list price
}
interface UsageSummary {
  days: number;
  usd_to_twd: number;
  unpriced: string[]; // models the price table does not know; their calls count as $0
  totals: Bucket;
  by_model: (Bucket & { model: string })[];
  by_source: (Bucket & { source: string })[];
  by_day: (Bucket & { day: string })[];
}

const RANGES = [7, 30, 90];

function usd(n: number): string {
  return `US$${n < 1000 ? n.toFixed(2) : Math.round(n).toLocaleString()}`;
}

function twd(n: number, rate: number): string {
  return `NT$${Math.round(n * rate).toLocaleString()}`;
}

/** Requests per day. One series, so magnitude is the whole job: a single
 * accent hue, no legend (the heading names it), values on hover. */
function DailyBars({ days, rate }: { days: (Bucket & { day: string })[]; rate: number }) {
  if (days.length === 0) return null;
  const peak = Math.max(...days.map((d) => d.requests), 1);
  return (
    <div className="usage-chart">
      <div className="usage-bars" role="img"
           aria-label={`Requests per day over the last ${days.length} active days`}>
        {days.map((d) => (
          <div key={d.day} className="usage-bar-col"
               title={`${d.day} — ${d.requests} requests, ${compact(d.total)} tokens, ${usd(d.cost_usd)} / ${twd(d.cost_usd, rate)}`}>
            <div className="usage-bar" style={{ height: `${(d.requests / peak) * 100}%` }} />
          </div>
        ))}
      </div>
      <div className="usage-axis">
        <span>{days[0]?.day.slice(5)}</span>
        <span>peak {peak}/day</span>
        <span>{days[days.length - 1]?.day.slice(5)}</span>
      </div>
    </div>
  );
}

/** Breakdown table with an inline magnitude bar — the label carries identity,
 * the bar carries share, so neither depends on color alone. */
function Breakdown<T extends Bucket>({
  title, rows, label, rate,
}: { title: string; rows: T[]; label: (r: T) => string; rate: number }) {
  const peak = Math.max(...rows.map((r) => r.requests), 1);
  return (
    <section className="usage-section">
      <div className="section-label">{title}</div>
      <table className="usage-table">
        <thead>
          <tr>
            <th scope="col">{title}</th>
            <th scope="col" className="num">Requests</th>
            <th scope="col" className="num">Tokens</th>
            <th scope="col" className="num">Cost</th>
            <th scope="col" className="share-cell">Share</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={label(r)}>
              <td className="name">{label(r)}</td>
              <td className="num">{r.requests.toLocaleString()}</td>
              <td className="num" title={`${r.prompt.toLocaleString()} in / ${r.output.toLocaleString()} out`}>
                {compact(r.total)}
              </td>
              <td className="num" title={twd(r.cost_usd, rate)}>{usd(r.cost_usd)}</td>
              <td className="share-cell">
                <div className="share">
                  <span style={{ width: `${(r.requests / peak) * 100}%` }} />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function Usage({ days = 30 }: { days?: number }) {
  const navigate = useNavigate();
  const setDays = (d: number) => navigate({ view: "usage", days: d }, { replace: true });
  const [data, setData] = useState<UsageSummary | null>(null);
  const [error, setError] = useState("");
  const [counted, setCounted] = useState(0);  // each time a character calls out the sum

  useEffect(() => {
    let alive = true;
    setData(null);
    setError("");
    usageApi<UsageSummary>(`/summary?days=${days}`)
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(String(e)));
    return () => {
      alive = false;
    };
  }, [days]);

  // In every state: loading, empty or failed, another range is the way on.
  const head = (
    <div className="usage-head">
      <div className="seg" role="group" aria-label="Time range">
        {RANGES.map((d) => (
          <button key={d} aria-pressed={days === d} onClick={() => setDays(d)}>
            {d}d
          </button>
        ))}
      </div>
    </div>
  );

  if (error || !data || data.totals.requests === 0) {
    return (
      <div className="usage-scroll">
        <div className="usage">
          {head}
          {error ? (
            <div className="notice">Couldn’t load usage: {error}</div>
          ) : !data ? (
            <>
              <div className="skeleton" style={{ height: 72 }} />
              <div className="skeleton" style={{ height: 160 }} />
            </>
          ) : (
            <div className="empty">
              <span style={{ color: "var(--accent)" }}><IconChart /></span>
              <h2>No model calls in this window</h2>
              <p>Usage covers chat skills, background runs (the Inbox, /plan, meeting
                notes) and Claude Code. Try a longer range.</p>
            </div>
          )}
        </div>
      </div>
    );
  }

  const { totals } = data;
  return (
    <div className="usage-scroll">
      <div className="usage">
        {head}

        <div className="stat-row">
          <div className={`stat stat-cost${counted ? " counted" : ""}`} title={`At list price; NT$ at ${data.usd_to_twd} per US$ (usage.usd_to_twd)`}>
            <span className="stat-label">Cost</span>
            <span className="stat-value" key={counted}>{usd(totals.cost_usd)}</span>
            <span className="stat-sub">{twd(totals.cost_usd, data.usd_to_twd)}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Requests</span>
            <span className="stat-value">{totals.requests.toLocaleString()}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Total tokens</span>
            <span className="stat-value">{compact(totals.total)}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Input</span>
            <span className="stat-value">{compact(totals.prompt)}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Output</span>
            <span className="stat-value">{compact(totals.output)}</span>
          </div>
          <div className="stat" title={`${totals.cached.toLocaleString()} of ${totals.prompt.toLocaleString()} input tokens read from cache`}>
            <span className="stat-label">Cached input</span>
            <span className="stat-value">
              {totals.prompt ? Math.round((totals.cached / totals.prompt) * 100) : 0}%
            </span>
          </div>
        </div>

        <section className="usage-section mk-floor">
          <div className="section-label">Requests per day</div>
          <DailyBars days={data.by_day} rate={data.usd_to_twd} />
          {character.Page && <character.Page page="usage" stand="floor" say={`${usd(totals.cost_usd)}!`}
                                             onBeat={() => setCounted((n) => n + 1)} />}
        </section>

        <Breakdown title="Model" rows={data.by_model} label={(r) => shortModel(r.model)} rate={data.usd_to_twd} />
        <Breakdown title="Source" rows={data.by_source} label={(r) => agentLabel(r.source)} rate={data.usd_to_twd} />

        <p className="usage-note">
          Counts every model call the server makes — chat sub-agents and background
          runs (the Inbox, /plan, meeting notes) alike. Output includes thinking tokens,
          which is how providers bill them. Cached input is the share of prompt
          tokens served from a prompt cache, billed at roughly a tenth of the
          normal rate. Cost is at list price from litellm's price table — cached
          input at the cache-read rate, long prompts at their tier — and in NT$
          at {data.usd_to_twd} per US$ (<code>usage.usd_to_twd</code> in project.yaml).
          {data.unpriced.length > 0 && <> Not priced (counted as US$0): {data.unpriced.join(", ")}.</>}
        </p>
      </div>
    </div>
  );
}
