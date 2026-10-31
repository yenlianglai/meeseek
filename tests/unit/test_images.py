"""Server-side image reading for callers that cannot send bytes (MCP hosts)."""

import pytest

from meeseek.runtime.images import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    ImageError,
    allowed_roots,
    collect_images,
    load_image_path,
)

PNG = b"\x89PNG\r\n\x1a\n"


def _png(path, extra=b"pixels"):
    path.write_bytes(PNG + extra)
    return str(path)


class TestPathContainment:
    """This runs on behalf of a remote caller, so an unconfined read would make
    the MCP port an exfiltration endpoint."""

    def test_reads_an_image_inside_an_allowed_root(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_IMAGE_ROOTS", str(tmp_path))
        mime, data = load_image_path(_png(tmp_path / "shot.png"))
        assert mime == "image/png"
        assert data == PNG + b"pixels"

    def test_refuses_a_path_outside_every_root(self, monkeypatch, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        inside = tmp_path / "inside"
        inside.mkdir()
        monkeypatch.setenv("AGENT_IMAGE_ROOTS", str(inside))
        with pytest.raises(ImageError, match="outside the directories"):
            load_image_path(_png(outside / "secret.png"))

    def test_refuses_a_symlink_that_escapes_a_root(self, monkeypatch, tmp_path):
        """The link sits inside the root; its target does not. Resolving before
        the check is the whole point."""
        secret = tmp_path / "elsewhere" / "secret.png"
        secret.parent.mkdir()
        _png(secret)
        root = tmp_path / "root"
        root.mkdir()
        link = root / "innocent.png"
        link.symlink_to(secret)

        monkeypatch.setenv("AGENT_IMAGE_ROOTS", str(root))
        with pytest.raises(ImageError, match="outside the directories"):
            load_image_path(str(link))

    def test_an_empty_allowlist_disables_paths_entirely(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_IMAGE_ROOTS", "")
        assert allowed_roots() == []
        with pytest.raises(ImageError, match="disabled"):
            load_image_path(_png(tmp_path / "shot.png"))

    def test_default_roots_cover_the_repo(self, monkeypatch):
        from meeseek.config import PROJECT_ROOT

        monkeypatch.delenv("AGENT_IMAGE_ROOTS", raising=False)
        assert PROJECT_ROOT.resolve() in allowed_roots()


class TestValidation:
    def test_rejects_a_type_the_model_cannot_read(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_IMAGE_ROOTS", str(tmp_path))
        path = tmp_path / "notes.pdf"
        path.write_bytes(b"%PDF-1.7")
        with pytest.raises(ImageError, match="not an image type"):
            load_image_path(str(path))

    def test_rejects_a_lying_extension(self, monkeypatch, tmp_path):
        """An extension is a claim; the magic bytes are the evidence."""
        monkeypatch.setenv("AGENT_IMAGE_ROOTS", str(tmp_path))
        path = tmp_path / "shot.png"
        path.write_bytes(b"GIF89a not really a png")
        with pytest.raises(ImageError, match="not really image/png"):
            load_image_path(str(path))

    def test_rejects_an_oversized_image(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_IMAGE_ROOTS", str(tmp_path))
        path = tmp_path / "huge.png"
        path.write_bytes(PNG + b"x" * (MAX_IMAGE_BYTES + 1))
        with pytest.raises(ImageError, match="the limit is"):
            load_image_path(str(path))

    def test_a_missing_file_says_so(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_IMAGE_ROOTS", str(tmp_path))
        with pytest.raises(ImageError, match="no such image"):
            load_image_path(str(tmp_path / "nope.png"))


class TestCollect:
    def test_collects_clipboard_first_then_paths(self, monkeypatch, tmp_path):
        import meeseek.runtime.images as images

        monkeypatch.setenv("AGENT_IMAGE_ROOTS", str(tmp_path))
        monkeypatch.setattr(images, "read_clipboard_image",
                            lambda: ("image/png", PNG + b"clip"))
        out = collect_images([_png(tmp_path / "a.png", b"a")], use_clipboard=True)
        assert out == [("image/png", PNG + b"clip"), ("image/png", PNG + b"a")]

    def test_no_sources_is_no_images(self, tmp_path):
        assert collect_images([], use_clipboard=False) == []
        assert collect_images(None, use_clipboard=False) == []

    def test_too_many_images_is_rejected_before_any_read(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_IMAGE_ROOTS", str(tmp_path))
        paths = [_png(tmp_path / f"{i}.png") for i in range(MAX_IMAGES + 1)]
        with pytest.raises(ImageError, match="the limit is"):
            collect_images(paths, use_clipboard=False)

    def test_one_bad_path_fails_the_whole_request(self, monkeypatch, tmp_path):
        """Answering about three of the four images the caller meant to show
        would be an answer to a different question."""
        monkeypatch.setenv("AGENT_IMAGE_ROOTS", str(tmp_path))
        with pytest.raises(ImageError):
            collect_images([_png(tmp_path / "ok.png"), "/etc/hosts"], use_clipboard=False)
