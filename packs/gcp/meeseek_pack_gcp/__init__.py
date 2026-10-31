"""meeseek's GCP pack: Cloud Logging, the GKE clusters' pods and events, and
Cloud Monitoring alerts, for the environments in `config.environments`, as an
MCP server (`python -m meeseek_pack_gcp`). Every call runs with the machine's
Application Default Credentials; no kubectl, no gcloud."""

from __future__ import annotations

from datetime import datetime

from meeseek_pack_kit import Tool, params

from meeseek_pack_gcp import gcp
from meeseek_pack_gcp.k8s import k8s_status, query_logs
from meeseek_pack_gcp.monitoring import _get, _subject_line, get_alert_policy, list_alerts


def tools() -> list[Tool]:
    _env_names = ", ".join(gcp.envs()) or "(none configured)"
    return [
        # --- Logs and pods, query built from project.yaml (k8s.py) ---
        Tool(
            name="query_logs",
            description=(
                "Read log lines of a service, newest first, as compact `time severity pod | message` lines in a "
                "code block. Name the environment and what you are after; the GCP project, cluster, namespace "
                "and the Logging filter are built for you (the filter used is printed, with a Cloud Console link). "
                "Start narrow — one service, the last 30 minutes — and widen only on a lead."
            ),
            fn=lambda env="", service="", contains="", severity="", since="30m", until="", limit=30,
            namespace="", pod="", resource_type="k8s_container", extra_filter="": query_logs(
                env, service, contains, severity, since, until, int(limit or 30), namespace, pod,
                resource_type, extra_filter),
            shows_raw=True,
            parameters=params({
                "env": {"type": "string", "description": f"Environment ({_env_names}) or part of one ('prod', 'uat'); empty = every env", "default": ""},
                "service": {"type": "string", "description": "Pod-name prefix or container name, e.g. 'acme-app-uat', 'litellm', 'backend'", "default": ""},
                "contains": {"type": "string", "description": "Text that must appear in the entry, e.g. an error string, session_id or ticket key", "default": ""},
                "severity": {"type": "string", "description": "Minimum severity: INFO, WARNING, ERROR, CRITICAL; empty = all", "default": ""},
                "since": {"type": "string", "description": "'15m', '2h', '1d', or an ISO time (UTC unless it has an offset)", "default": "30m"},
                "until": {"type": "string", "description": "End of the window, same forms as since; empty = now", "default": ""},
                "limit": {"type": "integer", "description": "Lines per environment (default 30, cap 50)", "default": 30},
                "namespace": {"type": "string", "description": "Only when not the env's default namespace (e.g. 'litellm')", "default": ""},
                "pod": {"type": "string", "description": "One exact pod name", "default": ""},
                "resource_type": {"type": "string", "description": "Logging resource type; empty = any (then cluster and namespace are not applied)", "default": "k8s_container"},
                "extra_filter": {"type": "string", "description": "An extra Logging-query clause, e.g. 'jsonPayload.event=\"llm_call\"'", "default": ""},
            }),
        ),
        Tool(
            name="k8s_status",
            description=(
                "Pods of an environment — phase, ready containers, restarts, why the last container died "
                "(OOMKilled, Error) — and the Warning events behind them, in code blocks. Start here for "
                "'pod 狀態', restarts, OOM, CrashLoopBackOff."
            ),
            fn=lambda env="", name="", namespace="", events_since="1h": k8s_status(env, name, namespace, events_since),
            shows_raw=True,
            parameters=params({
                "env": {"type": "string", "description": f"Environment ({_env_names}) or part of one; empty = every env", "default": ""},
                "name": {"type": "string", "description": "Only pods whose name contains this, e.g. 'acme-app-prod'", "default": ""},
                "namespace": {"type": "string", "description": "Only when not the env's default namespace", "default": ""},
                "events_since": {"type": "string", "description": "How far back Warning events go: '1h', '1d'", "default": "1h"},
            }),
        ),
        # --- Cloud Monitoring alerts（唯讀；不走 gcp_monitoring MCP，理由見 monitoring.py） ---
        Tool(
            name="list_alerts",
            description=(
                "List Cloud Monitoring alert incidents (newest first) across the configured "
                f"environments ({_env_names}). Start here for 「有沒有 alert / 現在燒什麼」 — it is the "
                "authoritative source, unlike the Chat channel the alerts are also piped into. "
                "Each line ends with the policy resource name; pass that to get_alert_policy for "
                "the runbook and threshold. Leave state empty to see recently-closed ones too, "
                "which is how flapping becomes visible."
            ),
            fn=lambda project="", state="", severity="", max_results=10: list_alerts(
                project, state, severity, int(max_results or 10)),
            parameters=params({
                "project": {"type": "string", "description": f"Env alias ({_env_names}) or GCP project id; empty = all envs", "default": ""},
                "state": {"type": "string", "description": "'OPEN' or 'CLOSED'; empty = both", "default": ""},
                "severity": {"type": "string", "description": "'CRITICAL', 'ERROR', 'WARNING'; empty = all", "default": ""},
                "max_results": {"type": "integer", "description": "Incidents per project (default 10, cap 50)", "default": 10},
            }),
        ),
        Tool(
            name="get_alert_policy",
            description=(
                "Read an alert policy — its runbook documentation (usually containing the exact "
                "log/metric query to run next) and the condition/threshold that fired. Use the "
                "policy name printed by list_alerts."
            ),
            fn=lambda policy, project="": get_alert_policy(policy, project),
            parameters=params({
                "policy": {"type": "string", "description": "'projects/<p>/alertPolicies/<id>' (as printed by list_alerts), or a bare id plus project"},
                "project": {"type": "string", "description": f"Env alias ({_env_names}) — only needed for a bare policy id", "default": ""},
            }, ["policy"]),
        ),
        Tool(
            name="alerts_poll",
            description="Open Cloud Monitoring incidents in every environment's project (a trigger source).",
            fn=alerts_poll,
            parameters=params({"since": {"type": "number"}, "state": {"type": "object"}}),
            source=True,
        ),
    ]


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def alerts_poll(since: float = 0, state: dict | None = None) -> dict:
    """Open incidents in every environment's project, as trigger items (each
    keyed by the incident, so one seen before is not filed again)."""
    import time

    out, errors = [], []
    now = time.time()
    for project in dict.fromkeys(e.project for e in gcp.resolve_envs("")):
        data = _get(f"projects/{project}/alerts", {"filter": 'state="OPEN"', "pageSize": 20,
                                                   "orderBy": "open_time desc"})
        if "error" in data:
            errors.append(f"{project}: {data['error']}")
            continue
        for a in data.get("alerts") or []:
            policy = a.get("policy", {})
            out.append({
                "kind": "alert", "key": f"alert:{a['name']}",
                "title": f"[{policy.get('severity') or '?'}] {policy.get('displayName', '?')}",
                "sender": project, "received_at": _ts(a.get("openTime", "")) or now,
                "link": f"https://console.cloud.google.com/monitoring/alerting/alerts/"
                        f"{a['name'].rsplit('/', 1)[-1]}?project={project}",
                "body": "", "meta": {"project": project, "opened": a.get("openTime", ""),
                                     "subject": _subject_line(a), "policy": policy.get("name", "")},
            })
    if errors and not out:
        raise RuntimeError("; ".join(errors))
    return {"items": out, "state": state or {}}


def _environments_text() -> str:
    """One line per environment: the ids a GCP or GKE tool asks for."""
    known = gcp.envs()
    if not known:
        return "- (no environments configured)"
    return "\n".join(
        f"- **{e.name}** — project `{e.project}`, location `{e.location}`, "
        f"cluster `{e.cluster}`, namespace `{e.namespace}`" for e in known.values())


def placeholders() -> dict[str, str]:
    return {"k8s_environments": _environments_text(),
            "k8s_env_names": ", ".join(gcp.envs()) or "(none configured)"}
