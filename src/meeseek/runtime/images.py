"""Getting images into a turn from an MCP caller.

Tool-call arguments are plain JSON and a host model cannot re-emit an image's
bytes, so the caller names an image and this module reads it server-side —
from a path (load_image_path) or the clipboard (read_clipboard_image).

These are local reads on behalf of a remote caller, so paths are confined to an
allowlist; otherwise an exposed MCP port would ship any file on this machine to
the model.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 4

# Extension → mime, limited to what Gemini accepts. Sniffed against the file's
# magic bytes below: an extension is a claim, not evidence.
_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

_MAGIC = {
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/webp": [b"RIFF"],  # RIFF....WEBP — the "WEBP" tag is checked below
}


class ImageError(Exception):
    """A rejection the caller can act on — surfaced as the tool's answer."""


def allowed_roots() -> list[Path]:
    """Directories an MCP caller may read images from.

    Defaults: the repo, meeseek's home, the temp dir, and Desktop/Downloads/Pictures.
    AGENT_IMAGE_ROOTS (os.pathsep-separated) overrides them; an empty value
    disables path-based attachment.
    """
    from meeseek.config import PROJECT_ROOT, home as meeseek_home

    env = os.environ.get("AGENT_IMAGE_ROOTS")
    if env is not None:
        return [Path(p).expanduser().resolve() for p in env.split(os.pathsep) if p.strip()]

    home = Path.home()
    roots = [PROJECT_ROOT, meeseek_home().root, Path(tempfile.gettempdir())]
    roots += [home / name for name in ("Desktop", "Downloads", "Pictures")]
    return [p.resolve() for p in roots if p.exists()]


def _sniff(data: bytes, claimed: str, name: str) -> str:
    """Confirm the bytes match the claimed type; HEIC/HEIF pass on extension."""
    magics = _MAGIC.get(claimed)
    if magics is None:
        return claimed
    if not any(data.startswith(m) for m in magics):
        raise ImageError(f"{name} is not really {claimed} — its contents say otherwise")
    if claimed == "image/webp" and data[8:12] != b"WEBP":
        raise ImageError(f"{name} is a RIFF file but not WebP")
    return claimed


def load_image_path(path: str) -> tuple[str, bytes]:
    """Read one image file, confined to allowed_roots(). Returns (mime, bytes).

    Symlinks are resolved before the containment check — otherwise a link
    planted inside an allowed root would read anything on the machine.
    """
    roots = allowed_roots()
    if not roots:
        raise ImageError("path-based image attachment is disabled on this server "
                         "(AGENT_IMAGE_ROOTS is empty)")

    p = Path(path).expanduser()
    try:
        resolved = p.resolve(strict=True)
    except OSError:
        raise ImageError(f"no such image: {path}") from None
    if not resolved.is_file():
        raise ImageError(f"not a file: {path}")
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ImageError(
            f"{resolved} is outside the directories this server may read images "
            f"from ({', '.join(str(r) for r in roots)}). Move or copy it into one "
            "of them, or set AGENT_IMAGE_ROOTS.")

    mime = _BY_SUFFIX.get(resolved.suffix.lower())
    if mime is None:
        raise ImageError(f"{resolved.name} is not an image type the model reads "
                         f"({', '.join(sorted(set(_BY_SUFFIX)))})")
    size = resolved.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ImageError(f"{resolved.name} is {size / 1e6:.1f} MB — the limit is "
                         f"{MAX_IMAGE_BYTES / 1e6:.0f} MB per image")

    data = resolved.read_bytes()
    return _sniff(data, mime, resolved.name), data


# AppleScript writes the clipboard's PNG flavour straight to a file: asking
# osascript to print it would mean parsing its hex «data PNGf…» notation.
_CLIPBOARD_SCRIPT = """
set out to POSIX file "{path}"
try
    set img to the clipboard as «class PNGf»
on error
    set img to the clipboard as «class TIFF»
end try
set fh to open for access out with write permission
set eof fh to 0
write img to fh
close access fh
"""


def read_clipboard_image() -> tuple[str, bytes]:
    """Grab the image on the *server machine's* clipboard. Returns (mime, bytes).

    macOS only. A capture lands as PNG or TIFF depending on how it was taken;
    TIFF is converted with sips because the model does not read it.
    """
    import shutil
    import sys

    if sys.platform != "darwin":
        raise ImageError("reading the clipboard is only implemented for macOS — "
                         "save the image to a file and pass its path instead")
    if not shutil.which("osascript"):
        raise ImageError("osascript is not available, so the clipboard cannot be read")

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "clipboard.bin"
        proc = subprocess.run(
            ["osascript", "-"],
            input=_CLIPBOARD_SCRIPT.format(path=raw),
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0 or not raw.exists() or raw.stat().st_size == 0:
            raise ImageError("there is no image on the clipboard — copy a "
                            "screenshot first (Cmd+Ctrl+Shift+4 captures "
                            "straight to it)")
        data = raw.read_bytes()
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        else:
            png = Path(tmp) / "clipboard.png"
            conv = subprocess.run(["sips", "-s", "format", "png", str(raw), "--out", str(png)],
                                  capture_output=True, text=True, timeout=30)
            if conv.returncode != 0 or not png.exists():
                raise ImageError("the clipboard image is in a format this server "
                                 f"could not convert to PNG: {conv.stderr.strip()}")
            data, mime = png.read_bytes(), "image/png"

        if len(data) > MAX_IMAGE_BYTES:
            raise ImageError(f"the clipboard image is {len(data) / 1e6:.1f} MB — the "
                             f"limit is {MAX_IMAGE_BYTES / 1e6:.0f} MB; scale it down first")
        return mime, data


def collect_images(paths: list[str] | None, use_clipboard: bool) -> list[tuple[str, bytes]]:
    """Resolve the image arguments of one request into (mime, bytes) pairs.

    Raises on the first rejection: a request that meant to show four images and
    silently showed three would get an answer about the wrong picture.
    """
    requested = list(paths or [])
    total = len(requested) + (1 if use_clipboard else 0)
    if total > MAX_IMAGES:
        raise ImageError(f"{total} images requested — the limit is {MAX_IMAGES} per request")

    images: list[tuple[str, bytes]] = []
    if use_clipboard:
        images.append(read_clipboard_image())
    images.extend(load_image_path(p) for p in requested)
    return images
