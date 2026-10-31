"""Unit tests for tools/image_gen.py — no network; the genai client is faked."""

from types import SimpleNamespace

import pytest

from meeseek.tools import image_gen


def _response(parts):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))])


def _image_part(data=b"\x89PNG\r\n\x1a\nfake", mime="image/png"):
    return SimpleNamespace(inline_data=SimpleNamespace(data=data, mime_type=mime), text=None)


def _text_part(text):
    return SimpleNamespace(inline_data=None, text=text)


class _FakeModels:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def out_dir(_home):
    return _home.outputs / "images"


def _use(monkeypatch, models):
    monkeypatch.setattr(image_gen, "_client", lambda: SimpleNamespace(models=models))
    return models


class TestGenerateImage:
    def test_saves_png_and_returns_path(self, out_dir, monkeypatch):
        models = _use(monkeypatch, _FakeModels(_response([_image_part()])))
        text = image_gen.generate_image("一隻戴著店員帽的橘貓，卡通風格", "16:9")
        files = list(out_dir.glob("image-*.png"))
        assert len(files) == 1 and files[0].read_bytes().startswith(b"\x89PNG")
        assert "outputs/images/" in text and "16:9" in text
        cfg = models.calls[0]["config"]
        assert cfg.image_config.aspect_ratio == "16:9"
        assert "IMAGE" in cfg.response_modalities

    def test_the_models_note_follows_the_picture(self, out_dir, monkeypatch):
        _use(monkeypatch, _FakeModels(_response([_text_part("here"), _image_part()])))
        out = image_gen.generate_image("a shop counter")
        assert "outputs/images/" in out and "Model note: here" in out

    def test_jpeg_gets_jpg_extension(self, out_dir, monkeypatch):
        _use(monkeypatch, _FakeModels(_response([_image_part(b"jpegbytes", "image/jpeg")])))
        image_gen.generate_image("x")
        assert list(out_dir.glob("*.jpg"))

    def test_no_image_part_is_reported_not_faked(self, out_dir, monkeypatch):
        _use(monkeypatch, _FakeModels(_response([_text_part("I can't draw that.")])))
        text = image_gen.generate_image("something blocked")
        assert "returned no image" in text and "I can't draw that." in text
        assert not out_dir.exists()

    def test_api_error_surfaces_real_cause(self, out_dir, monkeypatch):
        _use(monkeypatch, _FakeModels(error=PermissionError("403 model not enabled")))
        text = image_gen.generate_image("x")
        assert "failed" in text and "403 model not enabled" in text

    def test_rejects_bad_aspect_ratio_before_calling(self, out_dir, monkeypatch):
        models = _use(monkeypatch, _FakeModels(_response([_image_part()])))
        text = image_gen.generate_image("x", "7:3")
        assert "Unsupported aspect_ratio" in text and not models.calls

    def test_empty_prompt(self, out_dir, monkeypatch):
        models = _use(monkeypatch, _FakeModels(_response([_image_part()])))
        assert "needs a prompt" in image_gen.generate_image("   ") and not models.calls


class TestReferenceImages:
    def test_path_references_become_parts_after_prompt(self, out_dir, monkeypatch, tmp_path):
        ref = tmp_path / "ref.png"
        ref.write_bytes(b"\x89PNG\r\n\x1a\nref")
        monkeypatch.setattr("meeseek.runtime.images.load_image_path", lambda p: ("image/png", ref.read_bytes()))
        models = _use(monkeypatch, _FakeModels(_response([_image_part()])))
        text = image_gen.generate_image("same cat, mopping", reference_images=[str(ref)])
        contents = models.calls[0]["contents"]
        assert contents[0] == "same cat, mopping"
        assert contents[1].inline_data.mime_type == "image/png"
        assert "1 reference image" in text

    def test_bad_reference_path_stops_before_calling(self, out_dir, monkeypatch):
        from meeseek.runtime.images import ImageError

        def _boom(p):
            raise ImageError(f"{p} is outside the directories this server may read images from")

        monkeypatch.setattr("meeseek.runtime.images.load_image_path", _boom)
        models = _use(monkeypatch, _FakeModels(_response([_image_part()])))
        text = image_gen.generate_image("x", reference_images=["/etc/passwd.png"])
        assert "Reference image problem" in text and "outside" in text
        assert not models.calls

    def test_attached_images_used_only_when_asked(self, out_dir, monkeypatch):
        models = _use(monkeypatch, _FakeModels(_response([_image_part()])))
        attached = [("image/jpeg", b"jpeg-bytes")]
        image_gen.generate_image("x", _attached_images=attached)
        assert models.calls[0]["contents"] == "x"  # not asked → plain prompt
        image_gen.generate_image("x", use_attached_images=True, _attached_images=attached)
        assert models.calls[1]["contents"][1].inline_data.mime_type == "image/jpeg"

    def test_asking_for_attached_images_with_none_attached_is_explained(self, out_dir, monkeypatch):
        models = _use(monkeypatch, _FakeModels(_response([_image_part()])))
        text = image_gen.generate_image("x", use_attached_images=True)
        assert "no attached images" in text and not models.calls

    def test_too_many_references(self, out_dir, monkeypatch):
        models = _use(monkeypatch, _FakeModels(_response([_image_part()])))
        attached = [("image/png", b"p")] * (image_gen.MAX_REFERENCE_IMAGES + 1)
        text = image_gen.generate_image("x", use_attached_images=True, _attached_images=attached)
        assert "Too many reference images" in text and not models.calls
