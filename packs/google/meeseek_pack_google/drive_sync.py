"""Incremental sync: Google Drive -> local Markdown files, for meeseek's knowledge base.

Re-downloads (in parallel) only files whose Drive modifiedTime differs from the
history file. meeseek names both the folder the files go to and the history
file; this module writes inside that folder only.
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from googleapiclient.errors import HttpError
from pathlib import Path

from meeseek_pack_google.auth import get_credentials
from meeseek_pack_google.gdrive import build_service, convert_file_to_md, list_folder_files, sanitize_filename
from meeseek_pack_google.settings import settings

_MAX_WORKERS = 5


def _drive_url(file_meta: dict) -> str:
    """Best-effort Drive web link for a file, by mimeType."""
    fid = file_meta["id"]
    mime = file_meta.get("mimeType", "")
    if mime == "application/vnd.google-apps.document":
        return f"https://docs.google.com/document/d/{fid}/edit"
    if mime == "application/vnd.google-apps.spreadsheet":
        return f"https://docs.google.com/spreadsheets/d/{fid}/edit"
    return f"https://drive.google.com/file/d/{fid}/view"


def _metadata_header(file_meta: dict) -> str:
    """A freshness header so the agent can judge how current a synced doc is.
    Uses Drive's modifiedTime: the local mtime resets on every re-sync or
    checkout and says nothing about the document's age.
    """
    modified = file_meta.get("modifiedTime", "")
    date = modified.split("T")[0] if modified else "unknown"
    return (
        "> **來源**：Google Drive · "
        f"[{file_meta['name']}]({_drive_url(file_meta)})\n"
        f"> **文件最後更新**：{date}（Drive modifiedTime：{modified or 'unknown'}）\n"
        "> *此為同步下來的快照；上面的日期是原始文件在 Drive 的最後編輯時間，"
        "用來判斷內容是否過時。*\n"
    )


def _with_metadata_header(md_content: str, file_meta: dict) -> str:
    """Insert the freshness header just below the leading `# title` line, so
    kb_read still shows the doc name at line 1."""
    header = _metadata_header(file_meta)
    if md_content.startswith("# "):
        title, _, rest = md_content.partition("\n")
        return f"{title}\n\n{header}\n{rest.lstrip(chr(10))}"
    return f"{header}\n{md_content}"


def _load_history(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_history(path: Path, history: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def _download_one(creds, file_meta: dict) -> tuple[dict, str | None, str | None]:
    """Download and convert one file -> (file_meta, md_content, error_msg).
    Each thread builds its own API clients."""
    drive_service = build_service("drive", "v3", creds)
    sheets_service = build_service("sheets", "v4", creds)
    docs_service = build_service("docs", "v1", creds)
    try:
        md_content = convert_file_to_md(
            drive_service, sheets_service, file_meta, docs_service=docs_service
        )
        return (file_meta, md_content, None)
    except Exception as e:
        return (file_meta, None, str(e))


def sync(dest: str, history_file: str) -> str:
    """Run incremental sync into `dest`, remembering what was fetched in
    `history_file`. Returns a summary string."""
    creds = get_credentials()
    drive_service = build_service("drive", "v3", creds)

    docs_dir = Path(dest)
    docs_dir.mkdir(parents=True, exist_ok=True)
    history_path = Path(history_file)
    history = _load_history(history_path)
    config = settings()

    all_files: list[dict] = []
    seen_ids: set[str] = set()
    unresolved_shortcuts: list[dict] = []
    for folder_id in config.folder_ids:
        for f in list_folder_files(drive_service, folder_id, unresolved_shortcuts):
            if f["id"] not in seen_ids:
                all_files.append(f)
                seen_ids.add(f["id"])

    for sid in config.extra_file_ids:
        if sid not in seen_ids:
            try:
                meta = drive_service.files().get(
                    fileId=sid,
                    fields="id, name, mimeType, modifiedTime",
                    supportsAllDrives=True,
                ).execute()
            except HttpError as e:
                print(f"  [skip] Cannot fetch extra file {sid}: {e.reason}")
                continue
            all_files.append(meta)
            seen_ids.add(sid)

    # Drop excluded files (superseded versions, docs owned elsewhere) and
    # prune local copies of anything the config no longer covers — a file
    # removed from folder_ids/extra_file_ids, newly excluded, or renamed on
    # Drive (the old filename would otherwise linger as a stale duplicate).
    excluded = set(config.exclude_file_ids)
    all_files = [f for f in all_files if f["id"] not in excluded]
    seen_ids -= excluded
    pruned = _prune_stale(history, seen_ids, docs_dir)

    to_download = []
    skipped = 0
    for file_meta in all_files:
        file_id = file_meta["id"]
        current_modified = file_meta["modifiedTime"]
        record = history.get(file_id, {})
        if record.get("modifiedTime") == current_modified:
            skipped += 1
        else:
            to_download.append(file_meta)

    if not to_download:
        if pruned:
            _save_history(history_path, history)
        summary = f"Sync: 0 updated, {skipped} skipped (all up to date)"
        return summary + _format_pruned(pruned) + _format_unresolved(unresolved_shortcuts)

    updated = 0
    errors = 0
    updated_names: list[str] = []
    skipped_unsupported: list[str] = []

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_download_one, creds, fm): fm for fm in to_download
        }
        for future in as_completed(futures):
            file_meta, md_content, error_msg = future.result()
            name = file_meta["name"]
            file_id = file_meta["id"]

            if error_msg:
                print(f"  [error] Failed to convert '{name}': {error_msg}")
                errors += 1
                continue

            if md_content is None:
                skipped_unsupported.append(f"{name} (mime: {file_meta.get('mimeType', '?')})")
                continue

            filename = sanitize_filename(name) + ".md"
            output_path = docs_dir / filename
            output_path.write_text(
                _with_metadata_header(md_content, file_meta), encoding="utf-8"
            )
            _remove_renamed(history.get(file_id), output_path, docs_dir)

            history[file_id] = {
                "name": name,
                "modifiedTime": file_meta["modifiedTime"],
                "local_path": str(output_path),
            }
            updated += 1
            updated_names.append(name)

    _save_history(history_path, history)

    lines = [f"Sync: {updated} updated, {skipped} unchanged"]
    if errors:
        lines[0] += f", {errors} errors"
    if updated_names:
        lines.append("Updated files:")
        for n in updated_names:
            lines.append(f"  - {n}")
    if skipped_unsupported:
        lines.append("Skipped (unsupported format):")
        for n in skipped_unsupported:
            lines.append(f"  - {n}")
    return "\n".join(lines) + _format_pruned(pruned) + _format_unresolved(unresolved_shortcuts)


def _local_path(record: dict | None, docs_dir: Path) -> Path | None:
    """Resolve a history record's local_path; a relative path resolves to docs_dir by filename."""
    if not record or not record.get("local_path"):
        return None
    p = Path(record["local_path"])
    return p if p.is_absolute() else docs_dir / p.name


def _remove_renamed(record: dict | None, new_path: Path, docs_dir: Path) -> None:
    """A Drive rename changes the local filename; delete the old copy."""
    old = _local_path(record, docs_dir)
    if old and old != new_path and old.parent == docs_dir and old.exists():
        old.unlink()


def _prune_stale(history: dict, current_ids: set[str], docs_dir: Path) -> list[str]:
    """Forget files the config no longer covers and delete their local .md.

    Only touches files inside docs_dir so a mis-set history can never delete
    anything outside the folder meeseek named.
    """
    pruned: list[str] = []
    for file_id in [fid for fid in history if fid not in current_ids]:
        record = history.pop(file_id)
        path = _local_path(record, docs_dir)
        if path and path.parent == docs_dir and path.exists():
            path.unlink()
        pruned.append(record.get("name", file_id))
    return pruned


def _format_pruned(pruned: list[str]) -> str:
    if not pruned:
        return ""
    lines = ["", "Removed (no longer in sync config):"]
    lines += [f"  - {n}" for n in pruned]
    return "\n".join(lines)


def _format_unresolved(unresolved: list[dict]) -> str:
    """One aggregated report of shortcuts whose targets the sync identity cannot read."""
    if not unresolved:
        return ""
    seen: set[str] = set()
    # sync 以使用者本人的 ADC 身分讀取——target 要分享給你自己的帳號
    lines = ["", "Unresolved shortcuts (share the target with your account, "
             "or remove the shortcut):"]
    for u in unresolved:
        if u["target_id"] in seen:
            continue
        seen.add(u["target_id"])
        lines.append(f"  - {u['name']} (target: {u['target_id']})")
    return "\n".join(lines)

