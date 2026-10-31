"""Files the console can create, read, update and delete.

The files are the store (agents read the directories directly); this module is
only a guarded way to change them:

  pinned    notes/pinned/*.md — injected into every agent, every turn
  notes     notes/** except pinned/, facts.md (edited per entry through
            runtime/learning_page.py) and the templates git owns
  meetings  sources/meetings/** — notes and transcripts of recorded meetings
  outputs   outputs/** — what the agents produced; read-only, not context

Drive sources and SKILL.md are left out on purpose: the next sync would
overwrite an edit to the former, and a SOP is changed in git.

Every write carries the etag the editor read and is refused if the file
changed since; a new file is refused if the path is taken.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from meeseek.config import OUTPUTS_URL_PREFIX
from meeseek.config.paths import home

MAX_TEXT_BYTES = 2 * 1024 * 1024
TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".html", ".mmd", ".yaml", ".yml", ".svg", ".drawio"}


@dataclass(frozen=True)
class Kind:
    root: Path
    recursive: bool
    suffixes: frozenset[str]          # what a new file may be called
    skip: tuple[str, ...] = ()        # paths under root that belong to another kind
    writable: bool = True


def kinds() -> dict[str, Kind]:
    return {
        "pinned": Kind(home().notes / "pinned", False, frozenset({".md"})),
        # examples/ and README.md are templates git owns, like SKILL.md.
        "notes": Kind(home().notes, True, frozenset({".md", ".txt"}),
                      skip=("pinned/", "facts.md", "examples/", "README.md")),
        "meetings": Kind(home().kb / "sources" / "meetings", True, frozenset({".md", ".txt"})),
        "outputs": Kind(home().outputs, True, frozenset(), writable=False),
    }


class Conflict(Exception):
    """The file changed since it was read, or already exists."""


def _kind(name: str) -> Kind:
    known = kinds()
    if name not in known:
        raise ValueError(f"unknown kind: {name}")
    return known[name]


def _resolve(kind: Kind, rel: str) -> Path:
    """A path inside the kind's root, or ValueError."""
    rel = (rel or "").strip().lstrip("/")
    if not rel or any(part.startswith(".") for part in Path(rel).parts):
        raise ValueError("invalid path")
    root = kind.root.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or (not kind.recursive and target.parent != root):
        raise ValueError("path is outside this kind")
    inside = target.relative_to(root).as_posix()
    if any(inside == s or inside.startswith(s) for s in kind.skip):
        raise ValueError("path belongs to another kind")
    return target


def etag(path: Path) -> str:
    st = path.stat()
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


def is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES


def list_files(kind_name: str) -> list[dict]:
    """Every file of a kind, newest first."""
    kind = _kind(kind_name)
    if not kind.root.exists():
        return []
    files = kind.root.rglob("*") if kind.recursive else kind.root.glob("*")
    out = []
    for p in files:
        if not p.is_file():
            continue
        rel = p.relative_to(kind.root).as_posix()
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        if any(rel == s or rel.startswith(s) for s in kind.skip):
            continue
        st = p.stat()
        out.append({"path": rel, "size": st.st_size, "mtime": st.st_mtime, "etag": etag(p),
                    "text": is_text(p),
                    "url": f"{OUTPUTS_URL_PREFIX}/{rel}" if kind_name == "outputs" else ""})
    return sorted(out, key=lambda f: f["mtime"], reverse=True)


def read(kind_name: str, rel: str) -> dict:
    path = _resolve(_kind(kind_name), rel)
    if not path.is_file():
        raise FileNotFoundError(rel)
    if not is_text(path):
        raise ValueError("not a text file")
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("file is too large to edit here")
    return {"path": rel, "text": path.read_text(encoding="utf-8", errors="replace"), "etag": etag(path)}


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write(kind_name: str, rel: str, text: str, base: str | None) -> dict:
    """Create (`base` None) or update (`base` = the etag read) a text file."""
    kind = _kind(kind_name)
    if not kind.writable:
        raise ValueError(f"{kind_name} is read-only")
    path = _resolve(kind, rel)
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("file is too large")
    if base is None:
        if path.suffix.lower() not in kind.suffixes:
            raise ValueError(f"a new file here must end in {', '.join(sorted(kind.suffixes))}")
        if path.exists():
            raise Conflict("a file with this name exists")
    else:
        if not path.is_file():
            raise Conflict("the file was deleted since it was read")
        if etag(path) != base:
            raise Conflict("the file changed since it was read")
        if not is_text(path):
            raise ValueError("not a text file")
    _write_atomic(path, text)
    return {"path": rel, "etag": etag(path)}


def delete(kind_name: str, rel: str, base: str) -> None:
    path = _resolve(_kind(kind_name), rel)
    if not path.is_file():
        raise FileNotFoundError(rel)
    if etag(path) != base:
        raise Conflict("the file changed since it was read")
    path.unlink()
    root = _kind(kind_name).root.resolve()
    parent = path.parent
    while parent != root and parent.is_relative_to(root) and not any(parent.iterdir()):
        parent.rmdir()  # a folder emptied by the delete goes with it
        parent = parent.parent
