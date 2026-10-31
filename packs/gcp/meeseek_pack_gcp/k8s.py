"""Logs and pod status for the environments in project.yaml, built by code.

The model names an environment and what it is looking for; the Logging filter,
the GCP project, the cluster and the namespace come from project.yaml. Results
are compact lines in a code block — the raw evidence the user reads first
(core/policies.py:Evidence makes sure the reply shows it).

Read-only: Cloud Logging `entries:list`, and GET on pods and events.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

from meeseek_pack_gcp import gcp

_LOG_API = "https://logging.googleapis.com/v2/entries:list"
_MAX_LOG_LINES = 50
_LINE_CHARS = 400
_SEVERITIES = ("DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY")
# jsonPayload keys worth a line, after the message itself.
_PAYLOAD_KEYS = ("event", "error", "latency_ms", "status", "session_id", "request_id",
                 "player_id", "npc_id")
_DURATION = re.compile(r"^(\d+)\s*([smhd])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def _start_time(since: str) -> datetime:
    """'30m' / '2h' / '1d', or an ISO timestamp (UTC if it has no offset)."""
    s = (since or "30m").strip()
    m = _DURATION.match(s)
    now = datetime.now(timezone.utc)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now - timedelta(**{_UNITS[unit]: n})
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_filter(env: gcp.Env, *, namespace: str = "", service: str = "", pod: str = "",
                 contains: str = "", severity: str = "", start: datetime,
                 end: datetime | None = None, resource_type: str = "k8s_container",
                 extra_filter: str = "") -> str:
    """The Cloud Logging query for one environment."""
    clauses = []
    if resource_type:
        clauses.append(f"resource.type={_quote(resource_type)}")
    if resource_type == "k8s_container":
        clauses.append(f"resource.labels.project_id={_quote(env.project)}")
        if env.cluster:
            clauses.append(f"resource.labels.cluster_name={_quote(env.cluster)}")
        clauses.append(f"resource.labels.namespace_name={_quote(namespace or env.namespace)}")
        if service:
            # Substring match: "acme-app-uat" covers every pod of that deployment,
            # "backend" every container of that name.
            clauses.append(f"(resource.labels.pod_name:{_quote(service)} OR "
                           f"resource.labels.container_name={_quote(service)})")
        if pod:
            clauses.append(f"resource.labels.pod_name={_quote(pod)}")
    if severity:
        sev = severity.upper()
        if sev not in _SEVERITIES:
            raise ValueError(f"severity 必須是 {', '.join(_SEVERITIES)} 其中之一")
        clauses.append(f"severity>={sev}")
    clauses.append(f'timestamp>="{start.isoformat().replace("+00:00", "Z")}"')
    if end:
        clauses.append(f'timestamp<="{end.isoformat().replace("+00:00", "Z")}"')
    if contains:
        clauses.append(_quote(contains))
    if extra_filter:
        clauses.append(f"({extra_filter})")
    return "\n".join(clauses)


def _message(entry: dict) -> str:
    if entry.get("textPayload"):
        return str(entry["textPayload"])
    payload = entry.get("jsonPayload") or entry.get("protoPayload") or {}
    if not isinstance(payload, dict):
        return str(payload)
    msg = payload.get("message") or payload.get("msg") or ""
    extras = [f"{k}={payload[k]}" for k in _PAYLOAD_KEYS if payload.get(k) not in (None, "")]
    if not msg and not extras:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return " ".join([str(msg), *extras]).strip()


def format_entry(entry: dict) -> str:
    """One log line: time, severity, pod, message — newlines folded."""
    ts = (entry.get("timestamp") or "")[:23].replace("T", " ")
    labels = (entry.get("resource") or {}).get("labels") or {}
    where = labels.get("pod_name") or labels.get("service_name") or entry.get("logName", "").rsplit("/", 1)[-1]
    text = " ⏎ ".join(line.strip() for line in _message(entry).splitlines() if line.strip())
    if len(text) > _LINE_CHARS:
        text = text[:_LINE_CHARS] + " …"
    return f"{ts} {entry.get('severity', 'DEFAULT'):<8} {where} | {text}"


def query_logs(env: str = "", service: str = "", contains: str = "", severity: str = "",
               since: str = "30m", until: str = "", limit: int = 30, namespace: str = "",
               pod: str = "", resource_type: str = "k8s_container", extra_filter: str = "") -> str:
    """Recent log lines of one service, newest first, in a code block."""
    try:
        envs = gcp.resolve_envs(env)
        start = _start_time(since)
        end = _start_time(until) if until else None
    except ValueError as e:
        return f"Error: {e}"
    limit = max(1, min(int(limit or 30), _MAX_LOG_LINES))
    out = []
    for e in envs:
        try:
            flt = build_filter(e, namespace=namespace, service=service, pod=pod, contains=contains,
                               severity=severity, start=start, end=end,
                               resource_type=resource_type, extra_filter=extra_filter)
        except ValueError as err:
            return f"Error: {err}"
        data = gcp.call(_LOG_API, body={"resourceNames": [f"projects/{e.project}"], "filter": flt,
                                        "orderBy": "timestamp desc", "pageSize": limit})
        scope = " · ".join(x for x in (e.name, namespace or e.namespace, service, severity and f">={severity.upper()}",
                                       f"since {since}") if x)
        if "error" in data:
            out.append(f"### {scope}\nError: Cloud Logging {data['error']}\nfilter:\n{flt}")
            continue
        entries = data.get("entries") or []
        more = " (more exist: narrow the query or the time window)" if data.get("nextPageToken") else ""
        link = ("https://console.cloud.google.com/logs/query;query="
                + urllib.parse.quote(flt, safe="") + f"?project={e.project}")
        body = "\n".join(format_entry(x) for x in entries) or "(no entries)"
        out.append(f"### {scope} — {len(entries)} line(s), newest first{more}\n"
                   f"```text\n{body}\n```\nfilter:\n```\n{flt}\n```\n[Cloud Console]({link})")
    return "\n\n".join(out)


def _age(ts: str) -> str:
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    mins = int((datetime.now(timezone.utc) - t).total_seconds() // 60)
    return f"{mins}m" if mins < 60 else f"{mins // 60}h" if mins < 1440 else f"{mins // 1440}d"


def format_pod(pod: dict) -> str:
    """name, phase, ready, restarts, the last termination reason, age."""
    meta, status = pod.get("metadata") or {}, pod.get("status") or {}
    cs = status.get("containerStatuses") or []
    ready = sum(1 for c in cs if c.get("ready"))
    restarts = sum(int(c.get("restartCount") or 0) for c in cs)
    last = [f"{c['name']}:{t['reason']}" for c in cs
            if (t := (c.get("lastState") or {}).get("terminated")) and t.get("reason")]
    waiting = [f"{c['name']}:{w['reason']}" for c in cs
               if (w := (c.get("state") or {}).get("waiting")) and w.get("reason")]
    phase = status.get("phase", "?")
    if meta.get("deletionTimestamp"):
        phase = "Terminating"
    note = " ".join(filter(None, [", ".join(waiting), last and "last: " + ", ".join(last)]))
    return (f"{meta.get('name', '?'):<52} {phase:<11} {ready}/{len(cs):<3} "
            f"{restarts:>4}  {_age(meta.get('creationTimestamp', '')):>4}  {note}").rstrip()


def k8s_status(env: str = "", name: str = "", namespace: str = "", events_since: str = "1h") -> str:
    """Pods (phase, ready, restarts, why the last one died) and recent Warning events."""
    try:
        envs = gcp.resolve_envs(env)
        since = _start_time(events_since)
    except ValueError as e:
        return f"Error: {e}"
    out = []
    for e in envs:
        ns = namespace or e.namespace
        pods = gcp.kube(e, f"/api/v1/namespaces/{ns}/pods")
        if "error" in pods:
            out.append(f"### {e.name} · {ns}\nError: Kubernetes API {pods['error']}")
            continue
        items = [p for p in pods.get("items") or [] if name in (p.get("metadata") or {}).get("name", "")]
        lines = [f"{'POD':<52} {'PHASE':<11} READY RESTARTS AGE  NOTE"] + [format_pod(p) for p in items]
        if not items:
            lines.append(f"(no pods{' matching ' + name if name else ''})")
        events = gcp.kube(e, f"/api/v1/namespaces/{ns}/events", {"fieldSelector": "type=Warning"})
        ev_lines = []
        for ev in sorted(events.get("items") or [], key=lambda v: v.get("lastTimestamp") or "", reverse=True):
            when = ev.get("lastTimestamp") or ev.get("eventTime") or ""
            obj = (ev.get("involvedObject") or {}).get("name", "")
            if name not in obj or not when or _start_time(when) < since:
                continue
            ev_lines.append(f"{when[:19].replace('T', ' ')} {ev.get('reason', '?'):<18} {obj} "
                            f"x{ev.get('count', 1)} | {(ev.get('message') or '').strip()[:300]}")
        if "error" in events:
            ev_lines = [f"Error: {events['error']}"]
        block = "\n".join(lines)
        evs = "\n".join(ev_lines[:20]) or f"(no Warning events in the last {events_since})"
        out.append(f"### {e.name} · {ns} — {len(items)} pod(s)\n```text\n{block}\n```\n"
                   f"Warning events, last {events_since}:\n```text\n{evs}\n```")
    return "\n\n".join(out)
