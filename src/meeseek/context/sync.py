"""The knowledge base's mirror of Google Drive: the Google pack's drive_sync,
into knowledge_base/sources/drive/.

The pack fetches only the files whose Drive modifiedTime changed since the
last sync (recorded in sync_history.json under the home), converts them to
Markdown with a freshness header, and removes local copies the config no
longer covers. Which folders and files: the pack's `config`.
"""

from __future__ import annotations

from meeseek.config.paths import home


def sync() -> str:
    """Run an incremental sync. Returns a summary string."""
    from meeseek.runtime import packs

    try:
        return packs.call("drive_sync", {"dest": str(home().sources / "drive"),
                                         "history_file": str(home().data / "sync_history.json")})
    except LookupError:
        return "No Google pack is running: the Drive mirror needs mcp_servers.google (pack: google)."


def main():
    print(sync())


if __name__ == "__main__":
    main()
