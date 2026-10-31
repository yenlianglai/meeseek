"""A picture of a page an agent wrote, for where a page cannot be opened —
a phone chat. Headless Chrome renders it at phone size; a page that is still
drawing gets a few seconds of virtual time first."""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import time
from pathlib import Path

from meeseek.config.paths import home

logger = logging.getLogger(__name__)


def shots_dir() -> Path:
    return home().data / "page_shots"


KEEP_DAYS = 30   # a picture is taken again when it is asked for again


def prune(now: float | None = None) -> int:
    """Remove pictures older than KEEP_DAYS; how many."""
    if not shots_dir().is_dir():
        return 0
    cutoff = (now or time.time()) - KEEP_DAYS * 86400
    old = [p for p in shots_dir().iterdir() if p.is_file() and p.stat().st_mtime < cutoff]
    for p in old:
        p.unlink(missing_ok=True)
    return len(old)


_MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def chrome() -> str:
    for c in (_MAC_CHROME, shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chrome")):
        if c and Path(c).exists():
            return c
    return ""


# Headless Chrome will not lay a page out narrower than 500 CSS px (a smaller
# window is cropped, not narrowed), so 500 is the phone; tall enough for a page.
def screenshot(page: Path, width: int = 500, height: int = 1600) -> Path | None:
    """A PNG of `page` at phone size (2x), or None when there is no Chrome or it failed."""
    exe = chrome()
    if not exe:
        logger.info("[page_shot] no Chrome to render %s", page.name)
        return None
    key = hashlib.sha1(f"{page.resolve()}:{page.stat().st_mtime_ns}:{width}x{height}".encode()).hexdigest()[:16]
    out = shots_dir() / f"{page.stem}-{key}.png"
    if out.exists():
        return out
    shots_dir().mkdir(parents=True, exist_ok=True)
    cmd = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
           "--force-device-scale-factor=2", f"--window-size={width},{height}",
           "--virtual-time-budget=4000", f"--screenshot={out}", page.resolve().as_uri()]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("[page_shot] %s: %s", page.name, e)
        return None
    if not out.exists() or out.stat().st_size == 0:
        return None
    _trim_bottom(out)
    return out


def _trim_bottom(png: Path, margin: int = 48, tolerance: int = 10) -> None:
    """Cut the empty band below a page shorter than the window: rows within a
    little of the bottom-left pixel's colour (a background with a faint
    gradient counts as empty), all the way across."""
    from PIL import Image, ImageChops

    with Image.open(png) as im:
        rgb = im.convert("RGB")
    bg = Image.new("RGB", rgb.size, rgb.getpixel((0, rgb.height - 1)))
    diff = ImageChops.difference(rgb, bg).convert("L").point(lambda v: 255 if v > tolerance else 0)
    box = diff.getbbox()
    if box and box[3] + margin < rgb.height:
        rgb.crop((0, 0, rgb.width, box[3] + margin)).save(png)
