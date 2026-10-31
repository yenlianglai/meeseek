"""The GCP pack's settings (project.yaml `mcp_servers.gcp.config`).

    environments       {name: {context, namespace, gcp_project}} — a GKE context
                       is gke_<project>_<location>_<cluster>
    default_namespace  the namespace of an environment that names none
    project            a GCP project whose alerts are read besides the
                       environments' own

Credentials: the machine's Application Default Credentials (cloud-platform).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from meeseek_pack_kit import config


@dataclass(frozen=True)
class K8sEnvironment:
    context: str = ""
    namespace: str = ""
    gcp_project: str = ""


@dataclass(frozen=True)
class Settings:
    environments: dict[str, K8sEnvironment] = field(default_factory=dict)
    default_namespace: str = ""
    project: str = ""


def settings() -> Settings:
    c = config()
    envs = {str(n): K8sEnvironment(context=str(e.get("context", "")), namespace=str(e.get("namespace", "")),
                                   gcp_project=str(e.get("gcp_project", "")))
            for n, e in (c.get("environments") or {}).items() if isinstance(e, dict)}
    return Settings(environments=envs, default_namespace=str(c.get("default_namespace", "")),
                    project=str(c.get("project", "")))
