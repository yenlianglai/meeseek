"""Cloud Monitoring alert tools — incidents and the policies behind them.

Uses the REST API directly: the `gcp_monitoring` MCP server's tool
declarations run 50–80K chars each (project.yaml filters it to
list_timeseries/query_range), while these two tools cost ~1K.

Alerts live at the *metrics scope* of each project, so each environment's
project is a separate query — there is no "all environments" call.
Read-only: this is `GET projects/*/alerts` and `GET projects/*/alertPolicies/*`,
nothing here can ack, silence, or edit an alert.
"""

from datetime import datetime, timezone

from meeseek_pack_gcp import gcp
from meeseek_pack_gcp.settings import settings

_API = "https://monitoring.googleapis.com/v3"

def _allowed_projects() -> set[str]:
    """The projects an alert query may name: each environment's, plus the agent's own."""
    return {*(e.project for e in gcp.envs().values()), settings().project}


def _get(path: str, params: dict | None = None) -> dict:
    """GET a Monitoring v3 resource. Returns {"error": ...} instead of raising."""
    return gcp.call(f"{_API}/{path}", params)


def _resolve_projects(project: str) -> list[str]:
    """Env name, part of one, or a project id → the project ids to query.
    Raises for anything outside project.yaml, naming what may be asked for."""
    p = (project or "").strip()
    allowed = _allowed_projects()
    if p in allowed:
        return [p]
    try:
        return list(dict.fromkeys(e.project for e in gcp.resolve_envs(p)))
    except ValueError as e:
        raise ValueError(f"{e}；或 GCP project：{', '.join(sorted(allowed))}") from None


def _age(open_time: str, close_time: str) -> str:
    """Human duration of an incident (open → close, or open → now)."""
    try:
        start = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
        end = (datetime.fromisoformat(close_time.replace("Z", "+00:00"))
               if close_time else datetime.now(timezone.utc))
    except ValueError:
        return "?"
    mins = int((end - start).total_seconds() // 60)
    if mins < 60:
        return f"{mins}m"
    if mins < 1440:
        return f"{mins // 60}h{mins % 60:02d}m"
    return f"{mins // 1440}d{(mins % 1440) // 60}h"


def _subject_line(alert: dict) -> str:
    """What the incident is *about*, beyond the policy name.

    Log-based metrics carry almost nothing in resource.labels (often just
    project_id, already in the section header) — the distinguishing detail sits
    in metric.labels, e.g. event=reranker.breaker_open. Two incidents of one
    policy look identical without it.
    """
    res = alert.get("resource", {})
    labels = res.get("labels", {})
    # The labels that identify *which* workload; project_id/location are noise.
    parts = [res.get("type", "?")]
    parts += [labels[k] for k in
              ("namespace_name", "pod_name", "container_name", "cluster_name",
               "service_name", "revision_name", "database_id", "instance_id")
              if labels.get(k)]
    metric = alert.get("metric", {})
    parts += [f"{k}={v}" for k, v in sorted((metric.get("labels") or {}).items())]
    if mtype := metric.get("type"):
        parts.append(mtype.rsplit("/", 1)[-1])
    return " / ".join(parts)


def list_alerts(project: str = "", state: str = "", severity: str = "",
                max_results: int = 10) -> str:
    """List Cloud Monitoring alert incidents, newest first.

    Args:
        project: env alias ('prod', 'uat') or project id; empty = every env
        state: 'OPEN' or 'CLOSED'; empty = both (a flapping alert only shows up here)
        severity: 'CRITICAL' / 'ERROR' / 'WARNING'; empty = all
        max_results: incidents per project (default 10, cap 50)
    """
    try:
        projects = _resolve_projects(project)
    except ValueError as e:
        return str(e)

    page_size = max(1, min(int(max_results or 10), 50))
    filters = []
    if state:
        filters.append(f'state="{state.upper()}"')
    if severity:
        filters.append(f'policy.severity="{severity.upper()}"')

    out: list[str] = []
    for proj in projects:
        params = {"pageSize": page_size, "orderBy": "open_time desc"}
        if filters:
            params["filter"] = " AND ".join(filters)
        data = _get(f"projects/{proj}/alerts", params)

        if "error" in data:
            out.append(f"**{proj}** — Monitoring API error: {data['error']}")
            continue
        alerts = data.get("alerts") or []
        total = data.get("totalSize")
        if not alerts:
            out.append(f"**{proj}** — no alerts matching.")
            continue

        header = f"**{proj}** — {len(alerts)} alert(s)"
        if total is not None and total > len(alerts):
            header += f" (of {total}; raise max_results for more)"
        out.append(header + ":")
        for a in alerts:
            policy = a.get("policy", {})
            sev = policy.get("severity") or "?"
            closed = a.get("closeTime", "")
            when = (a.get("openTime") or "?")[:19].replace("T", " ")
            out.append(
                f"- [{a.get('state', '?')}] {sev} — {policy.get('displayName', '?')}\n"
                f"  opened {when}Z, lasted {_age(a.get('openTime', ''), closed)}"
                f"{'' if closed else ' (still open)'}\n"
                f"  on {_subject_line(a)}\n"
                f"  policy: {policy.get('name', '?')}"
            )

    return "\n".join(out)


def get_alert_policy(policy: str, project: str = "") -> str:
    """Read an alert policy: its runbook documentation and firing conditions.

    This is where the *why* lives — the documentation field usually carries the
    exact log/metric query to run next, and the condition gives the threshold
    that tripped. Feed it the `policy:` line from list_alerts.

    Args:
        policy: full name 'projects/<p>/alertPolicies/<id>', or just the id
        project: env alias or project id — only needed when policy is a bare id
    """
    name = policy.strip()
    if not name.startswith("projects/"):
        try:
            projects = _resolve_projects(project)
        except ValueError as e:
            return str(e)
        if len(projects) > 1:
            return (f"policy '{policy}' 是裸 id，需要指定 project（可用: "
                    f"{', '.join(gcp.envs())}）")
        name = f"projects/{projects[0]}/alertPolicies/{name.rsplit('/', 1)[-1]}"

    data = _get(name)
    if "error" in data:
        return f"Monitoring API error: {data['error']}"

    lines = [
        f"**{data.get('displayName', '?')}**",
        f"- Name: {data.get('name', '?')}",
        f"- Severity: {data.get('severity', 'n/a')} | "
        f"Enabled: {data.get('enabled', '?')} | "
        f"Combiner: {data.get('combiner', '?')}",
    ]
    if labels := data.get("userLabels"):
        lines.append(f"- Labels: {', '.join(f'{k}={v}' for k, v in labels.items())}")
    if channels := data.get("notificationChannels"):
        lines.append(f"- Notifies: {len(channels)} channel(s)")

    doc = (data.get("documentation") or {}).get("content", "")
    if doc:
        lines.append(f"\n**Runbook**\n{doc[:2000]}")

    for cond in data.get("conditions") or []:
        lines.append(f"\n**Condition: {cond.get('displayName', '?')}**")
        threshold = (cond.get("conditionThreshold")
                     or cond.get("conditionMatchedLog")
                     or cond.get("conditionAbsent") or {})
        if flt := threshold.get("filter"):
            lines.append(f"```\n{flt}\n```")
        for key in ("comparison", "thresholdValue", "duration"):
            if key in threshold:
                lines.append(f"- {key}: {threshold[key]}")
            elif key == "thresholdValue" and "comparison" in threshold:
                # proto3 drops a zero threshold from the JSON; absent means 0,
                # not "no threshold" — so COMPARISON_GT alone reads as "> 0".
                lines.append("- thresholdValue: 0 (unset in the API response)")
        for agg in threshold.get("aggregations") or []:
            lines.append(
                f"- aggregation: {agg.get('alignmentPeriod', '?')} "
                f"{agg.get('perSeriesAligner', '')} "
                f"{agg.get('crossSeriesReducer', '')}".rstrip()
            )
    return "\n".join(lines)
