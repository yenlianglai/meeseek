"""Mermaid diagram rendering to a PNG under outputs/diagrams/, with local `mmdc`.

The diagram source may describe confidential architecture, so it never leaves
the machine: there is no external-service fallback. If `mmdc` is missing or the
render fails, the error is returned with the source preserved.
"""

import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from meeseek.config import outputs_url
from meeseek.config.paths import home

_DEFAULT_THEME = "neutral"
# mmdc's valid themes; anything else falls back to the default so a bad theme
# name never fails the render.
_MMDC_THEMES = {"default", "neutral", "forest", "dark", "base"}
_MMDC_TIMEOUT = 60  # seconds; local render of a normal diagram is well under this

_VALID_DIAGRAM_TYPES = re.compile(
    r"^\s*(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram|erDiagram|"
    r"gantt|pie|gitGraph|journey|mindmap|timeline|quadrantChart|xychart|block)",
    re.MULTILINE,
)


def _check_syntax(mermaid: str) -> str | None:
    """Basic local syntax check. Returns error message or None."""
    if not mermaid.strip():
        return "Empty diagram source."
    if not _VALID_DIAGRAM_TYPES.search(mermaid):
        return (
            "No valid diagram type found. Must start with one of: "
            "graph, flowchart, sequenceDiagram, classDiagram, stateDiagram, "
            "erDiagram, gantt, pie, gitGraph, journey, mindmap, timeline."
        )
    return None


def _render_local(mermaid: str, theme: str) -> bytes:
    """Render with local `mmdc` (mermaid-cli) and return PNG bytes.

    Raises RuntimeError with a specific, honest message when the CLI is missing
    or the render fails.
    """
    mmdc = shutil.which("mmdc")
    if not mmdc:
        raise RuntimeError(
            "mermaid-cli (mmdc) is not installed on this host — install it with "
            "`npm i -g @mermaid-js/mermaid-cli` to enable diagram rendering."
        )
    safe_theme = theme if theme in _MMDC_THEMES else _DEFAULT_THEME
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "diagram.mmd"
        out = Path(tmp) / "diagram.png"
        src.write_text(mermaid, encoding="utf-8")
        try:
            proc = subprocess.run(
                [mmdc, "-i", str(src), "-o", str(out),
                 "-t", safe_theme, "-b", "white"],
                capture_output=True, text=True, timeout=_MMDC_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"mmdc timed out after {_MMDC_TIMEOUT}s") from e
        if proc.returncode != 0 or not out.exists():
            detail = (proc.stderr or proc.stdout or "no output").strip()[:500]
            raise RuntimeError(f"mmdc exited {proc.returncode}: {detail}")
        return out.read_bytes()


def render_mermaid(mermaid: str, theme: str = _DEFAULT_THEME) -> str:
    """Render a Mermaid diagram to a local PNG file and return its path.

    Args:
        mermaid: Mermaid diagram syntax (e.g. "graph TD\\n  A-->B")
        theme: Theme: neutral (default), forest, dark, default
    """
    error = _check_syntax(mermaid)
    if error:
        return (
            f"Diagram syntax error: {error}\n\n"
            f"Please fix and try again.\n\n"
            f"{mermaid}"
        )

    try:
        png = _render_local(mermaid, theme)
    except RuntimeError as e:
        # Keep the source so the human can retry or paste it into mermaid.live.
        return (
            f"Diagram render failed: {e}\n"
            f"The diagram source itself is below — nothing was sent anywhere:\n\n"
            f"```mermaid\n{mermaid}\n```"
        )

    if not png.startswith(b"\x89PNG"):
        detail = png[:400].decode("utf-8", "replace").strip()
        return (
            f"Diagram render failed: renderer returned a non-PNG response — "
            f"likely a syntax error the local check missed:\n{detail}\n\n"
            f"```mermaid\n{mermaid}\n```"
        )

    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", theme).strip("-") or "diagram"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = home().outputs / "diagrams"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"diagram-{slug}-{stamp}.png"
    path.write_bytes(png)

    rel = home().rel(path)
    # The embed uses the served outputs URL (a repo-relative src would resolve
    # against the console's /ui/ page and 404). The absolute path is included for
    # tools that need to open the file.
    return (
        f"![Diagram]({outputs_url(path)})\n\n"
        f"Rendered diagram → {rel} ({len(png)} bytes). This is a local PNG file — "
        f"embed or attach it directly (no external hotlink). "
        f"Absolute path, for tools that need to open it: {path}"
    )
