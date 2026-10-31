"""Image generation with Gemini's native image model, saved as a local PNG.

Gemini-only, unlike meeseek's other model calls (runtime/oneshot.py): no
gateway or other provider takes this request, so it goes to Vertex AI directly
and says so when `models.image` is not a Gemini image model.

A read tool attached to every conversational agent (see MEDIA_TOOLS in
tools/registry.py), for concept art, UI mock-ups, NPC/character reference
sheets and illustrations. Not for architecture or flow diagrams — those use
render_diagram, which keeps the source on the machine.

The prompt is sent to Vertex AI in the project's own GCP project (same ADC as
every other model call), so keep confidential specifics out of prompts.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from meeseek.config import cfg, outputs_url
from meeseek.config.paths import home

# runtime.images is imported lazily inside _reference_parts: the runtime
# package's __init__ imports tools.registry, which imports this module.

MAX_REFERENCE_IMAGES = 6  # Gemini image models take a handful; more just dilutes

# Gemini image models accept these; anything else is rejected with a 400 that
# names none of them, so validate up front and tell the model what is allowed.
ASPECT_RATIOS = ("1:1", "3:4", "4:3", "9:16", "16:9", "2:3", "3:2", "4:5", "5:4", "21:9")
_DEFAULT_ASPECT = "1:1"
_TIMEOUT_MS = 120_000


def _client():
    """One genai client per call; cheap, and keeps auth on the project's ADC."""
    from google import genai

    return genai.Client(
        vertexai=True,
        project=cfg.google_cloud.project,
        location=cfg.google_cloud.location or "global",
        http_options={"timeout": _TIMEOUT_MS},
    )


def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^\w一-鿿-]+", "-", text, flags=re.UNICODE).strip("-")
    return (s[:n].rstrip("-") or "image").lower()


def _reference_parts(
    reference_images: list[str] | None,
    attached: list[tuple[str, bytes]] | None,
) -> tuple[list, list[str]]:
    """Turn reference inputs into genai Parts. Returns (parts, problems).

    Paths go through runtime.images.load_image_path — the same allowlist the
    MCP surface uses, so a prompt can't make the tool read arbitrary files.
    `attached` is (mime, bytes) already taken from the current turn.
    """
    from google.genai import types

    from meeseek.runtime.images import ImageError, load_image_path

    parts: list = []
    problems: list[str] = []
    for mime, data in attached or []:
        parts.append(types.Part.from_bytes(data=data, mime_type=mime))
    for raw in reference_images or []:
        path = (raw or "").strip()
        if not path:
            continue
        try:
            mime, data = load_image_path(path)
        except ImageError as e:
            problems.append(str(e))
            continue
        parts.append(types.Part.from_bytes(data=data, mime_type=mime))
    return parts, problems


def generate_image(
    prompt: str,
    aspect_ratio: str = _DEFAULT_ASPECT,
    reference_images: list[str] | None = None,
    use_attached_images: bool = False,
    _attached_images: list[tuple[str, bytes]] | None = None,
):
    """Generate an image from a text prompt (plus optional reference images).

    Args:
        prompt: What the picture should show — subject, style, composition,
            mood. With references, say how to use them ("same character as the
            reference, now mopping the floor", "match this colour palette").
        aspect_ratio: One of 1:1 (default), 3:4, 4:3, 9:16, 16:9, 2:3, 3:2,
            4:5, 5:4, 21:9.
        reference_images: Paths to local image files to use as references
            (character sheets, style samples, a previous generation). Read only
            from the allowed image roots (project, temp, Desktop/Downloads/
            Pictures, or AGENT_IMAGE_ROOTS).
        use_attached_images: True to also pass the images the user attached to
            this turn as references.
        _attached_images: internal — (mime, bytes) of the turn's attached
            images, passed in by the engine (engine.py:registry_spec).
    """
    if not prompt or not prompt.strip():
        return "Image generation needs a prompt describing the picture."
    if not cfg.models.image.startswith("gemini"):
        return (f"Error: image generation runs on a Gemini image model (on Vertex AI, with the machine's Google "
                f"login); models.image is {cfg.models.image!r}. Nothing was generated.")
    ratio = (aspect_ratio or _DEFAULT_ASPECT).strip()
    if ratio not in ASPECT_RATIOS:
        return (
            f"Unsupported aspect_ratio {ratio!r}. Use one of: {', '.join(ASPECT_RATIOS)}."
        )
    if use_attached_images and not _attached_images:
        return (
            "use_attached_images=true but this turn has no attached images. Ask the user to "
            "attach one, or pass reference_images with file paths instead."
        )
    ref_parts, problems = _reference_parts(
        reference_images, _attached_images if use_attached_images else None
    )
    if problems:
        return "Reference image problem(s) — nothing generated:\n- " + "\n- ".join(problems)
    if len(ref_parts) > MAX_REFERENCE_IMAGES:
        return f"Too many reference images ({len(ref_parts)}); the limit is {MAX_REFERENCE_IMAGES}."

    from google.genai import types

    contents = [prompt, *ref_parts] if ref_parts else prompt
    try:
        client = _client()  # keep a reference: a temporary Client is closed on GC mid-request
        response = client.models.generate_content(
            model=cfg.models.image,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=ratio),
            ),
        )
    except Exception as e:  # noqa: BLE001 — surface the real cause, don't guess
        return (
            f"Image generation failed ({cfg.models.image}): {type(e).__name__}: {e}\n"
            "Nothing was saved. If this is a permission or model-availability error, "
            "the GCP project needs the model enabled; otherwise retry with a simpler prompt."
        )

    png: bytes | None = None
    mime = "image/png"
    notes: list[str] = []
    for cand in getattr(response, "candidates", None) or []:
        for part in getattr(getattr(cand, "content", None), "parts", None) or []:
            blob = getattr(part, "inline_data", None)
            if blob is not None and getattr(blob, "data", None) and png is None:
                png = blob.data
                mime = getattr(blob, "mime_type", None) or mime
            elif getattr(part, "text", None):
                notes.append(part.text.strip())
    if png is None:
        why = " ".join(notes)[:400] or "the model returned no image part (possibly a safety block)."
        return (
            f"Image generation returned no image ({cfg.models.image}): {why}\n"
            "Rephrase the prompt — avoid real people, brands and sensitive content."
        )

    ext = "jpg" if "jpeg" in mime else "png"
    out_dir = home().outputs / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"image-{_slug(prompt)}-{stamp}.{ext}"
    path.write_bytes(png)
    rel = home().rel(path)

    refs = f", {len(ref_parts)} reference image(s)" if ref_parts else ""
    text = (
        f"![Generated image]({outputs_url(path)})\n\n"
        f"Generated with {cfg.models.image} ({ratio}{refs}) → {rel} ({len(png)} bytes). "
        "This is a local file — embed or attach it directly."
    )
    if notes:
        text += "\n\nModel note: " + " ".join(notes)[:300]
    return text
