"""project.yaml files written before packs.

A vendor's settings used to be a top-level section the core read (`jira:`).
They are now a pack's `config:` under `mcp_servers`. `upgrade` rewrites an
old file's sections into pack entries as it loads, so a deployment keeps its
tools without editing the file; a file that already names the pack is left
alone. Delete a branch here once nobody's file has that section.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# section → (pack, environment variables other MCP servers read from it)
_SECTIONS: dict[str, tuple[str, dict[str, Any]]] = {
    "jira": ("jira", {
        "JIRA_URL": lambda s: s.get("base_url", ""),
        # Atlassian Cloud serves Confluence under the Jira site.
        "CONFLUENCE_URL": lambda s: f"{s['base_url'].rstrip('/')}/wiki" if s.get("base_url") else "",
    }),
    "gitlab": ("gitlab", {"GITLAB_URL": lambda s: s.get("base_url", "")}),
    "kubernetes": ("gcp", {}),
    "google_drive": ("google", {}),
}

# triggers.<flag>: true → the pack source that now serves it: (name, tool, every_s, {arg: old key})
_SOURCES = {
    "alerts": ("alert", "alerts_poll", 0, {}),
    "gmail": ("gmail", "gmail_poll", 0, {"query": "gmail_query"}),
    "chat": ("chat", "chat_poll", 0, {}),
    "calendar": ("calendar", "calendar_poll", 300, {"lead_minutes": "calendar_lead_minutes"}),
}
_GOOGLE = {"gmail", "chat", "calendar"}


def _sources(raw: dict[str, Any]) -> dict[str, Any]:
    triggers = raw.get("triggers")
    if not isinstance(triggers, dict) or not any(flag in triggers for flag in _SOURCES):
        return raw
    triggers = dict(triggers)
    sources = dict(triggers.get("sources") or {})
    google = False
    for flag, (name, tool, every_s, args) in _SOURCES.items():
        on = triggers.pop(flag, False)
        values = {arg: triggers.pop(key) for arg, key in args.items() if key in triggers}
        if on and name not in sources:
            sources[name] = {"tool": tool, **({"every_s": every_s} if every_s else {}),
                             **({"args": values} if values else {})}
            google = google or flag in _GOOGLE
    triggers["sources"] = sources
    raw = {**raw, "triggers": triggers}
    if google and "google_drive" not in raw:     # the Google pack serves these sources
        raw["google_drive"] = {}
    return raw


def upgrade(raw: dict[str, Any]) -> dict[str, Any]:
    """`raw` with each old vendor section moved into a pack entry."""
    raw = _sources(raw)
    if not any(section in raw for section in _SECTIONS):
        return raw
    raw = dict(raw)
    if isinstance(raw.get("kubernetes"), dict):    # the GCP pack also reads alerts in this project
        project = (raw.get("google_cloud") or {}).get("project", "")
        raw["kubernetes"] = {**raw["kubernetes"], **({"project": project} if project else {})}
    servers = dict(raw.get("mcp_servers") or {})
    packs = {(s or {}).get("pack") for s in servers.values() if isinstance(s, dict)}
    for section, (pack, exported) in _SECTIONS.items():
        settings = raw.pop(section, None)
        if not isinstance(settings, dict):
            continue
        for name, get in exported.items():
            value = get(settings)
            if value:
                os.environ[name] = str(value)
        if pack in packs:
            continue
        servers[pack] = {"pack": pack, "config": settings}
        logger.info("project.yaml: `%s:` is now mcp_servers.%s (pack %s); read as such", section, pack, pack)
    raw["mcp_servers"] = servers
    return raw
