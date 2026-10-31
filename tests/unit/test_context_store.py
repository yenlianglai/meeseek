"""Context files: created, read, updated and deleted inside their own
directory only, and never overwritten blind."""

import pytest

from meeseek.runtime import context_store as ctx


@pytest.fixture(autouse=True)
def roots(_home):
    notes = _home.notes
    (notes / "pinned").mkdir(parents=True)
    (notes / "facts.md").write_text("# Learned Facts\n")
    (notes / "pinned" / "team.md").write_text("team")
    (_home.outputs / "images").mkdir(parents=True)
    (_home.outputs / "images" / "a.png").write_bytes(b"\x89PNG")
    return _home.kb


def test_the_full_cycle():
    made = ctx.write("notes", "sync/2026-10-10.md", "# 初稿", None)
    assert [f["path"] for f in ctx.list_files("notes")] == ["sync/2026-10-10.md"]
    read = ctx.read("notes", "sync/2026-10-10.md")
    assert read["text"] == "# 初稿" and read["etag"] == made["etag"]
    updated = ctx.write("notes", "sync/2026-10-10.md", "# 定稿", read["etag"])
    assert ctx.read("notes", "sync/2026-10-10.md")["text"] == "# 定稿"
    ctx.delete("notes", "sync/2026-10-10.md", updated["etag"])
    assert ctx.list_files("notes") == []
    assert not (ctx.kinds()["notes"].root / "sync").exists()   # the emptied folder went with it


def test_a_stale_edit_is_refused(roots):
    first = ctx.read("pinned", "team.md")
    (roots / "notes" / "pinned" / "team.md").write_text("changed in the IDE, longer")
    with pytest.raises(ctx.Conflict):
        ctx.write("pinned", "team.md", "mine", first["etag"])
    with pytest.raises(ctx.Conflict):
        ctx.delete("pinned", "team.md", first["etag"])


def test_creating_over_an_existing_file_is_refused():
    with pytest.raises(ctx.Conflict):
        ctx.write("pinned", "team.md", "x", None)


@pytest.mark.parametrize("kind,path", [
    ("notes", "../outside.md"), ("notes", "a/../../outside.md"), ("notes", ".hidden.md"),
    ("notes", "facts.md"), ("notes", "pinned/team.md"),        # another kind's
    ("notes", "examples/repos.md"), ("notes", "README.md"),     # git's
    ("pinned", "sub/deeper.md"),                                # pinned is flat
])
def test_paths_stay_inside_their_kind(kind, path):
    with pytest.raises(ValueError):
        ctx.read(kind, path)


def test_new_files_have_an_allowed_name():
    with pytest.raises(ValueError):
        ctx.write("pinned", "script.sh", "rm -rf /", None)


def test_facts_and_pinned_notes_are_not_listed_as_notes():
    ctx.write("notes", "a.md", "a", None)
    assert [f["path"] for f in ctx.list_files("notes")] == ["a.md"]


def test_outputs_are_read_only():
    with pytest.raises(ValueError):
        ctx.write("outputs", "reports/x.md", "x", None)


def test_binary_outputs_are_listed_and_deletable_not_editable():
    [png] = ctx.list_files("outputs")
    assert png["path"] == "images/a.png" and not png["text"] and png["url"].endswith("images/a.png")
    with pytest.raises(ValueError):
        ctx.read("outputs", "images/a.png")
    ctx.delete("outputs", "images/a.png", png["etag"])
    assert ctx.list_files("outputs") == []
