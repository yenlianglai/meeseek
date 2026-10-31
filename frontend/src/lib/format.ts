/** Formatting shared across views. */

/** Elapsed seconds → "42s" / "3m 07s"-style label for live and finished runs. */
export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}

/** 81869703 → "81.9M". Big token counts are for comparing, not auditing. */
export function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/**
 * Drop the provider/region prefix and version tail so `gemini-3.6-flash` and
 * `us.anthropic.claude-opus-5` both read at a glance in a narrow column.
 */
export function shortModel(model: string): string {
  return model.replace(/^[a-z]{2}\.anthropic\./, "").replace(/-\d{8}-v\d+:\d+$/, "");
}

/**
 * `sprint_agent` → "sprint", `plan_my_day` → "plan my day". The `_agent`
 * suffix is a wiring detail no reader cares about. Case is left alone:
 * title-casing would turn `acme_app_orchestrator` into "Acme App Orchestrator".
 */
export function agentLabel(id: string): string {
  return id.replace(/_agent$/, "").replace(/_/g, " ");
}

/** How long ago, coarse: now, 5m, 3h, 2d. */
export function relTime(epochSec: number): string {
  const diff = Date.now() / 1000 - epochSec;
  if (diff < 60) return "now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}
