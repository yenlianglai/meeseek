"""Configuration: project.yaml for settings, .env for secrets.

    config/schema.py  — the dataclasses, and the only home of a default value
    config/loader.py  — yaml → dataclasses, plus the env vars SDKs read
    config/paths.py   — code locations, and the Home your context lives in

Every dataclass in the schema's `__all__` is re-exported here.

`cfg` is the current Config, looked up on each attribute access: whatever
`set_config()` gave the process, or `using_config()` gave this context, else
project.yaml and .env, read the first time anything asks. Nothing is read on
import. Secrets are env-only and have no yaml field: `secret("GITLAB_TOKEN")`.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Iterator

from meeseek.config.schema import *  # noqa: F401,F403 — every dataclass, per schema.__all__
from meeseek.config.schema import Config
from meeseek.config.loader import build_config, load_dotenv_once  # noqa: F401
from meeseek.config.paths import (  # noqa: F401
    OUTPUTS_URL_PREFIX,
    PROJECT_ROOT,
    PROMPTS_DIR,
    Home,
    home,
    outputs_url,
    set_home,
    using_home,
)

_scoped: ContextVar[Config | None] = ContextVar("meeseek_config", default=None)
_process: Config | None = None


def load() -> Config:
    """Read .env and project.yaml, and make the result the process's config."""
    global _process
    load_dotenv_once()
    _process = build_config()
    return _process


def config() -> Config:
    return _scoped.get() or _process or load()


def set_config(c: Config | None) -> None:
    """The config for the whole process (threads included); None reads project.yaml again on next use."""
    global _process
    _process = c


@contextmanager
def using_config(c: Config) -> Iterator[Config]:
    token = _scoped.set(c)
    try:
        yield c
    finally:
        _scoped.reset(token)


def secret(name: str) -> str:
    """A secret from the environment (.env included)."""
    config()
    return os.environ.get(name, "")


class _Current:
    """`cfg.x` is `config().x`, so a module that imported `cfg` sees the config of now."""

    __slots__ = ()

    def __getattr__(self, name: str):
        return getattr(config(), name)

    def __setattr__(self, name: str, value) -> None:
        raise AttributeError("cfg is read-only: build a Config and set_config() or using_config() it")

    def __repr__(self) -> str:
        return f"cfg -> {config()!r}"


if TYPE_CHECKING:
    cfg: Config
else:
    cfg = _Current()
