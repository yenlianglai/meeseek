"""Unit tests for context/sync.py: exclusion, pruning, and rename cleanup."""

import json

from google_test_settings import use
from meeseek_pack_google import drive_sync as sync_mod
from meeseek_pack_google.drive_sync import _prune_stale, _remove_renamed


class TestPruneStale:
    def test_removes_history_and_local_file_for_dropped_ids(self, tmp_path):
        docs = tmp_path / "drive"
        docs.mkdir()
        old = docs / "old.md"
        old.write_text("x")
        history = {
            "keep": {"name": "Keep", "local_path": str(docs / "keep.md")},
            "drop": {"name": "Drop", "local_path": str(old)},
        }
        pruned = _prune_stale(history, {"keep"}, docs)
        assert pruned == ["Drop"]
        assert "drop" not in history and "keep" in history
        assert not old.exists()

    def test_resolves_legacy_relative_paths_by_filename(self, tmp_path):
        docs = tmp_path / "drive"
        docs.mkdir()
        (docs / "legacy.md").write_text("x")
        history = {"a": {"name": "Legacy", "local_path": "knowledge_base/legacy.md"}}
        _prune_stale(history, set(), docs)
        assert not (docs / "legacy.md").exists()

    def test_never_deletes_outside_docs_dir(self, tmp_path):
        docs = tmp_path / "drive"
        docs.mkdir()
        outside = tmp_path / "elsewhere.md"
        outside.write_text("x")
        history = {"a": {"name": "A", "local_path": str(outside)}}
        _prune_stale(history, set(), docs)
        assert outside.exists()


class TestRemoveRenamed:
    def test_deletes_previous_filename_after_drive_rename(self, tmp_path):
        old = tmp_path / "API 文件 (持續討論中).md"
        old.write_text("x")
        new = tmp_path / "API 文件 (6月版本討論用).md"
        _remove_renamed({"local_path": str(old)}, new, tmp_path)
        assert not old.exists()

    def test_keeps_file_when_name_unchanged(self, tmp_path):
        same = tmp_path / "doc.md"
        same.write_text("x")
        _remove_renamed({"local_path": str(same)}, same, tmp_path)
        assert same.exists()


class _FakeFiles:
    def __init__(self, metas):
        self._metas = metas

    def get(self, fileId, **_):
        meta = self._metas[fileId]
        return type("R", (), {"execute": lambda self: meta})()


class _FakeDrive:
    def __init__(self, metas):
        self._files = _FakeFiles(metas)

    def files(self):
        return self._files


class TestSyncEndToEnd:
    def test_excluded_files_are_skipped_and_their_local_copy_removed(self, tmp_path, monkeypatch):
        docs = tmp_path / "sources" / "drive"
        docs.mkdir(parents=True)
        history_file = tmp_path / "data" / "sync_history.json"
        history_file.parent.mkdir(parents=True, exist_ok=True)
        stale = docs / "Excluded Doc.md"
        stale.write_text("old")
        history_file.write_text(json.dumps({
            "ex": {"name": "Excluded Doc", "modifiedTime": "t0", "local_path": str(stale)},
        }))

        folder_files = [
            {"id": "ex", "name": "Excluded Doc", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "t1"},
            {"id": "ok", "name": "Kept Doc", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "t1"},
        ]
        use(monkeypatch, folder_ids=["folder"], extra_file_ids=[], exclude_file_ids=["ex"])
        monkeypatch.setattr(sync_mod, "get_credentials", lambda: object())
        monkeypatch.setattr(sync_mod, "build_service", lambda *a, **k: _FakeDrive({}))
        monkeypatch.setattr(sync_mod, "list_folder_files", lambda svc, fid, unresolved=None: list(folder_files))
        monkeypatch.setattr(
            sync_mod, "convert_file_to_md",
            lambda drive, sheets, meta, docs_service=None: f"# {meta['name']}\n\nbody",
        )

        summary = sync_mod.sync(str(docs), str(history_file))

        assert "1 updated" in summary
        assert "Excluded Doc" in summary and "Removed" in summary
        assert not stale.exists()
        assert (docs / "Kept Doc.md").exists()
        history = json.loads(history_file.read_text())
        assert set(history) == {"ok"}


def test_the_unresolved_report_names_each_shortcut_once():
    from meeseek_pack_google.drive_sync import _format_unresolved

    out = _format_unresolved([{"name": "doc.gdoc", "target_id": "abc123"}, {"name": "again", "target_id": "abc123"}])
    assert "doc.gdoc" in out and "abc123" in out and "again" not in out
    assert _format_unresolved([]) == ""


def test_the_settings_come_from_the_client(monkeypatch):
    from google_test_settings import use
    from meeseek_pack_google.settings import settings

    use(monkeypatch, folder_ids=["a"], exclude_file_ids=["x", "y"])
    assert settings().folder_ids == ["a"] and settings().exclude_file_ids == ["x", "y"]
    use(monkeypatch, exclude_file_ids=[])
    assert settings().exclude_file_ids == []
