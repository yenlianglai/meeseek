"""The GitLab pack's settings (project.yaml `mcp_servers.gitlab.config`).

    base_url         https://gitlab.example.com
    projects         the repo paths the tools may reach; the first is the default
    skip_ssl_verify  for a host with a private CA
    local_clone      a local clone of the primary repo ({{local_clone}}: where
                     the code skill hands Claude Code a change)

Secret: GITLAB_TOKEN (read_api + read_repository to read; api to comment and edit MRs).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from meeseek_pack_kit import config


@dataclass(frozen=True)
class Settings:
    base_url: str = ""
    projects: list[str] = field(default_factory=list)
    skip_ssl_verify: bool = False
    local_clone: str = ""

    @property
    def project_path(self) -> str:
        """Primary repo (first in the list) — default for all GitLab tools."""
        return self.projects[0] if self.projects else ""


def settings() -> Settings:
    c = config()
    return Settings(base_url=str(c.get("base_url", "")).rstrip("/"), projects=list(c.get("projects") or []),
                    skip_ssl_verify=bool(c.get("skip_ssl_verify", False)), local_clone=str(c.get("local_clone", "")))
