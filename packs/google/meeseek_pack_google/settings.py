"""The Google pack's settings (project.yaml `mcp_servers.google.config`).

    folder_ids          Drive folders whose files the knowledge base mirrors
    extra_file_ids      single files to mirror besides the folders'
    exclude_file_ids    files reached through the folders that are not
                        mirrored (superseded versions, docs owned elsewhere);
                        a sync also deletes their local copy
    meetings_folder_id  a Drive folder of meeting recordings (/meetings)

Credentials: the machine's Application Default Credentials with the Workspace
scopes (auth.py). Everything here reads; nothing writes to Workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from meeseek_pack_kit import config


@dataclass(frozen=True)
class Settings:
    folder_ids: list[str] = field(default_factory=list)
    extra_file_ids: list[str] = field(default_factory=list)
    exclude_file_ids: list[str] = field(default_factory=list)
    meetings_folder_id: str = ""


def settings() -> Settings:
    c = config()
    return Settings(folder_ids=list(c.get("folder_ids") or []), extra_file_ids=list(c.get("extra_file_ids") or []),
                    exclude_file_ids=list(c.get("exclude_file_ids") or []),
                    meetings_folder_id=str(c.get("meetings_folder_id", "")))
