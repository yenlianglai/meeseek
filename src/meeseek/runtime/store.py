"""Line-delimited JSON on disk: MCP task records, rule reviews, the rule archive.

One JSON object per line. Locks are per path and process-local: they
serialise this process's own threads (concurrent turns can append to the
same file at the same moment), not other processes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def path_lock(path: Path) -> threading.Lock:
    """The lock for one file, shared by every caller that touches it.

    Read-modify-write on a text file (facts.md, learned.md) needs this too,
    which is why it is public rather than an internal of JsonlStore.
    """
    key = Path(path).resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


class JsonlStore:
    """An append-only JSONL file, with rotation and a whole-file rewrite."""

    def __init__(self, path: Path, *, max_bytes: int | None = None) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes

    # -- write ---------------------------------------------------------------

    def append(self, record: dict) -> None:
        """Add one record. Never raises: losing a log line must not fail a turn."""
        try:
            with path_lock(self.path):
                self._rotate_if_needed()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("could not append to %s", self.path, exc_info=True)

    def update(self, fn: Callable[[list[dict]], Iterable[dict] | None]) -> None:
        """Read every record, hand them to `fn`, write back what it returns.

        Held under the lock for the whole read-modify-write, and written through
        a temporary file so a crash mid-write cannot truncate the store. `fn`
        returning None means "no change".
        """
        with path_lock(self.path):
            records = self._read_unlocked()
            updated = fn(records)
            if updated is None:
                return
            self._write_all_unlocked(list(updated))

    # -- read ----------------------------------------------------------------

    def read(self) -> list[dict]:
        """Every record. A malformed line is skipped, not fatal — a half-written
        line from a killed process must not make the whole store unreadable."""
        with path_lock(self.path):
            return self._read_unlocked()

    # -- internals -----------------------------------------------------------

    def _read_unlocked(self) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def _write_all_unlocked(self, records: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
            encoding="utf-8")
        os.replace(tmp, self.path)

    def _rotate_if_needed(self) -> None:
        if self.max_bytes is None or not self.path.exists():
            return
        if self.path.stat().st_size <= self.max_bytes:
            return
        backup = self.path.with_suffix(self.path.suffix + ".old")
        try:
            backup.unlink(missing_ok=True)
            self.path.rename(backup)
        except Exception:
            logger.debug("could not rotate %s", self.path, exc_info=True)
