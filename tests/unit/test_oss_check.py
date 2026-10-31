"""The release carries nothing on the private list (scripts/oss_check.py)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("oss_check", ROOT / "scripts" / "oss_check.py")
oss_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oss_check)


def test_the_check_finds_a_name_in_contents_and_in_file_names(tmp_path):
    (tmp_path / "a.md").write_text("hello\nwritten by Somebody\n")
    (tmp_path / "somebody.txt").write_text("fine\n")
    pats = [__import__("re").compile("(?i)somebody")]
    hits = oss_check.findings(["a.md", "somebody.txt"], pats, root=tmp_path)
    assert hits == ["a.md:2: (?i)somebody", "somebody.txt: file name matches (?i)somebody"]


@pytest.mark.skipif(not oss_check.LIST.exists(), reason="no private .oss-forbidden on this machine")
def test_the_tracked_tree_is_clean():
    assert oss_check.findings(oss_check.tracked(), oss_check.patterns()) == []


def _export_module():
    spec = importlib.util.spec_from_file_location("export_oss", ROOT / "scripts" / "export_oss.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_export_is_the_tracked_tree_in_one_commit(tmp_path, monkeypatch):
    import re
    import subprocess

    export = _export_module()
    monkeypatch.setattr(export.oss_check, "patterns", lambda: [re.compile(r"(?!x)x")])   # matches nothing
    dest = tmp_path / "oss"
    export.export(dest, "Release Bot <release@example.com>")
    git = lambda *a: subprocess.run(["git", *a], cwd=dest, capture_output=True, text=True, check=True).stdout  # noqa: E731
    assert git("rev-list", "--count", "HEAD").strip() == "1"
    assert git("log", "-1", "--format=%an <%ae>|%cn <%ce>").strip() == (
        "Release Bot <release@example.com>|Release Bot <release@example.com>")
    exported = set(git("ls-files").split())
    tracked = set(subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=ROOT,
                                 capture_output=True, text=True, check=True).stdout.split())
    kept = export.oss_check.internal()
    assert exported == {f for f in tracked if not f.startswith(kept)}
    assert not {"project.yaml", ".env", ".oss-forbidden"} & exported
    assert not any(f.startswith(kept) for f in exported), "the internal modules stay on GitLab"
    if (dest / ".mcp.json").exists():
        import json

        assert not any(any(k in json.dumps(s) for k in kept)
                       for s in json.loads((dest / ".mcp.json").read_text())["mcpServers"].values())


def test_the_export_refuses_what_the_release_check_finds(tmp_path, monkeypatch):
    import re

    export = _export_module()
    monkeypatch.setattr(export.oss_check, "patterns", lambda: [])
    with pytest.raises(SystemExit, match="nothing to look for"):
        export.export(tmp_path / "a", "A <a@b.c>")
    monkeypatch.setattr(export.oss_check, "patterns", lambda: [re.compile("meeseek")])
    with pytest.raises(SystemExit, match="oss_check.py finds"):
        export.export(tmp_path / "b", "A <a@b.c>")
    monkeypatch.setattr(export.oss_check, "patterns", lambda: [re.compile(r"(?!x)x")])
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "x").write_text("")
    with pytest.raises(SystemExit, match="not empty"):
        export.export(tmp_path / "c", "A <a@b.c>")



def test_the_internal_list_names_the_animation_module():
    assert {"frontend/src/animation/", "frontend/motion/"} <= set(oss_check.internal())
