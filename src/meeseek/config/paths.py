"""Where things live.

Code locations (the package, its prompts, the agents) are constants. Everything
meeseek reads or writes on your behalf — the knowledge base, its caches and
stores, the skills it learns into — lives under a `Home`, resolved when a path
is used rather than when a module is imported:

    MEESEEK_HOME     the root: knowledge_base/ and .cache/ under it (default: the checkout)
    MEESEEK_SKILLS   the skills folder (default: <checkout>/skills)

`set_home()` replaces it for the process (tests point it at a temp dir);
`using_home()` replaces it for one context, so two apps in one process can each
have their own. Nothing here touches the disk at import; `Home.ensure()` creates
the folders at startup.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
# Running from a clone of the repo, not an installed package: the checkout is
# then the default home, and holds project.yaml, .env and the skills.
CHECKOUT = (PROJECT_ROOT / "pyproject.toml").exists()


def default_root() -> Path:
    """Where your context lives unless MEESEEK_HOME says: the checkout, or ~/.meeseek."""
    return Path(os.environ.get("MEESEEK_HOME") or (PROJECT_ROOT if CHECKOUT else Path.home() / ".meeseek")).expanduser()
# Every engine-owned prompt, read by name through context.prompts.text().
PROMPTS_DIR = PACKAGE_ROOT / "prompts"
# Where the outputs folder is reachable from a browser: server.py mounts it
# there read-only. A repo-relative embed would resolve against /ui/… and 404.
OUTPUTS_URL_PREFIX = "/outputs"


@dataclass(frozen=True)
class Home:
    """The folders meeseek keeps your context in."""

    root: Path
    skills: Path

    @property
    def kb(self) -> Path:
        return self.root / "knowledge_base"

    @property
    def sources(self) -> Path:
        return self.kb / "sources"

    @property
    def notes(self) -> Path:
        return self.kb / "notes"

    @property
    def outputs(self) -> Path:
        return self.kb / "outputs"

    @property
    def data(self) -> Path:
        """Stores, logs and caches: what meeseek writes for itself."""
        return self.root / ".cache"

    @property
    def sessions_db(self) -> Path:
        return self.data / "sessions.db"

    @property
    def learning(self) -> Path:
        return self.data / "learning"

    def rel(self, path: Path) -> str:
        """`path` as stored in records: relative to the root when it is under it."""
        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(resolved)

    def ensure(self) -> None:
        for d in (self.data, self.sources, self.notes, self.outputs):
            d.mkdir(parents=True, exist_ok=True)


def default_home() -> Home:
    root = default_root()
    skills = Path(os.environ.get("MEESEEK_SKILLS") or (PROJECT_ROOT if CHECKOUT else root) / "skills").expanduser()
    return Home(root=root, skills=skills)


_scoped: ContextVar[Home | None] = ContextVar("meeseek_home", default=None)
_process: Home | None = None


def home() -> Home:
    return _scoped.get() or _process or default_home()


def set_home(h: Home | None) -> None:
    """The home for the whole process (threads included); None goes back to the default."""
    global _process
    _process = h


@contextmanager
def using_home(h: Home) -> Iterator[Home]:
    token = _scoped.set(h)
    try:
        yield h
    finally:
        _scoped.reset(token)


def outputs_url(path: Path) -> str:
    """The browser URL for a file under the outputs folder.

    Falls back to the home-relative (or absolute) path for anything outside it.
    """
    resolved = Path(path).resolve()
    try:
        return f"{OUTPUTS_URL_PREFIX}/{resolved.relative_to(home().outputs.resolve()).as_posix()}"
    except ValueError:
        return home().rel(resolved)
