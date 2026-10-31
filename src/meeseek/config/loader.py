"""Configuration loader — YAML → dataclasses, plus the env vars third parties read.

The loader carries **no default values of its own**. It takes whatever
project.yaml supplies, coerces it to each dataclass's field types, and leaves
everything else to the dataclass default (config/schema.py);
tests/unit/test_config.py enforces this.

What it does own:

  * **nesting** — `_normalize_k8s` turns the nested environments into their
    dataclasses.
  * **env exports** — `EXPORTED_ENV`. The google-genai SDK, gcloud and the
    `${VAR}` interpolation in `mcp_servers[].args_from_env` read process
    environment, not our config, so the yaml values are published there. yaml
    wins over an env var of the same name.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import typing
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from meeseek.config.schema import (
    CoachMeeting,
    Config,
    GoogleCloudConfig,
    MCPServerConfig,
    MediaConfig,
    ModelsConfig,
    ProjectConfig,
    TriggersConfig,
    MessengersConfig,
    ClaudeConfig,
    UsageConfig,
    LearningConfig,
    WorkflowsConfig,
)

from meeseek.config.paths import CHECKOUT, PROJECT_ROOT, default_root  # single source of truth

logger = logging.getLogger(__name__)

_dotenv_loaded = False


def load_dotenv_once() -> None:
    """Secrets from .env into the environment; a variable already set wins."""
    global _dotenv_loaded
    if not _dotenv_loaded:
        load_dotenv((PROJECT_ROOT if CHECKOUT else default_root()) / ".env")
        _dotenv_loaded = True


def _load_yaml() -> dict[str, Any]:
    config_path = Path(os.environ.get("PROJECT_CONFIG") or (PROJECT_ROOT if CHECKOUT else default_root()) / "project.yaml")
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


# ---------------------------------------------------------------------------
# Generic dict → dataclass
# ---------------------------------------------------------------------------

_SCALARS = {int: int, float: float, str: str, bool: bool}


def _coerce(value: Any, annotation: Any) -> Any:
    """Best-effort cast of a yaml scalar to the field's declared type.

    yaml gives `cpus: 1` as an int where the field is a str, and quoted numbers
    as str where the field is an int. Anything that will not convert is passed
    through untouched — a loud failure later beats a silently wrong value here.
    """
    origin = typing.get_origin(annotation)
    if origin in (list, dict) or annotation in (list, dict):
        return value
    caster = _SCALARS.get(annotation)
    if caster is None or isinstance(value, caster):
        return value
    if caster is bool:
        return bool(value)
    try:
        return caster(value)
    except (TypeError, ValueError):
        return value


def _from_mapping(cls, raw: dict[str, Any] | None, section: str, **overrides):
    """Build `cls` from a yaml section: present keys only, defaults for the rest.

    A key the dataclass does not declare is logged, so a stale setting or typo
    is visible.
    """
    raw = raw or {}
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(raw) - set(fields) - set(overrides))
    if unknown:
        logger.warning(
            "project.yaml: %s has no field(s) %s — ignored (typo, or a setting that was removed)",
            section, ", ".join(unknown))
    kwargs = {
        name: _coerce(raw[name], fields[name].type)
        for name in fields
        if name in raw and name not in overrides
    }
    return cls(**kwargs, **overrides)


def _coach_meetings(raw) -> list[CoachMeeting]:
    """Each entry a name, or {name, focus}."""
    out = []
    for i, entry in enumerate(raw or []):
        if isinstance(entry, str):
            out.append(CoachMeeting(name=entry))
        elif isinstance(entry, dict) and entry.get("name"):
            out.append(_from_mapping(CoachMeeting, entry, f"triggers.coach_meetings[{i}]"))
        else:
            logger.warning("project.yaml: triggers.coach_meetings[%d] needs a name — ignored", i)
    return out


def _mcp_servers(raw: dict) -> list[MCPServerConfig]:
    servers = [
        _from_mapping(MCPServerConfig, data, f"mcp_servers.{name}", name=name)
        for name, data in (raw or {}).items() if isinstance(data, dict)
    ]
    for s in servers:
        if s.pack and not s.agents:
            s.agents = ["*"]
    return servers


# ---------------------------------------------------------------------------
# Env exports — yaml is the source; these are for third parties that read env
# ---------------------------------------------------------------------------

EXPORTED_ENV: tuple[tuple[str, Any], ...] = (
    ("GOOGLE_CLOUD_PROJECT", lambda c: c.google_cloud.project),
    ("GOOGLE_CLOUD_LOCATION", lambda c: c.google_cloud.location),
    ("GOOGLE_GENAI_USE_VERTEXAI", lambda c: "TRUE" if c.google_cloud.vertex else "FALSE"),
    ("GCP_PROJECT_ID", lambda c: c.google_cloud.quota_project),
)


def _export_env(cfg: Config) -> None:
    """Publish yaml values the SDKs read from the environment.

    Only non-empty values are exported, so a project.yaml that does not set a
    field leaves any pre-existing env var alone; a value set in yaml wins over
    the shell.
    """
    for name, get in EXPORTED_ENV:
        value = get(cfg)
        if value:
            os.environ[name] = str(value)


def build_config(raw: dict[str, Any] | None = None) -> Config:
    """Build a Config from a raw project.yaml mapping (or load it from disk)."""
    from meeseek.config import legacy

    if raw is None:
        raw = _load_yaml()
    raw = legacy.upgrade(raw)

    unknown_sections = sorted(set(raw) - {f.name for f in dataclasses.fields(Config)})
    if unknown_sections:
        logger.warning("project.yaml: unknown top-level section(s) %s — ignored",
                       ", ".join(unknown_sections))

    cfg = Config(
        project=_from_mapping(ProjectConfig, raw.get("project"), "project"),
        google_cloud=_from_mapping(GoogleCloudConfig, raw.get("google_cloud"), "google_cloud"),
        workflows=_from_mapping(WorkflowsConfig, raw.get("workflows"), "workflows"),
        models=_from_mapping(ModelsConfig, raw.get("models"), "models"),
        media=_from_mapping(MediaConfig, raw.get("media"), "media"),
        usage=_from_mapping(UsageConfig, raw.get("usage"), "usage"),
        learning=_from_mapping(LearningConfig, raw.get("learning"), "learning"),
        triggers=_from_mapping(TriggersConfig, raw.get("triggers"), "triggers",
                               coach_meetings=_coach_meetings((raw.get("triggers") or {}).get("coach_meetings"))),
        messengers=_from_mapping(MessengersConfig, raw.get("messengers"), "messengers"),
        claude=_from_mapping(ClaudeConfig, raw.get("claude"), "claude"),
        mcp_servers=_mcp_servers(raw.get("mcp_servers")),
    )
    _export_env(cfg)
    return cfg
