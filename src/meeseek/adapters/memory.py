"""In-memory adapters: for tests, and for a run that should leave nothing behind."""

from __future__ import annotations

import threading
import time
from typing import Sequence

from meeseek.core.events import Event


class MemoryEventStore:
    def __init__(self) -> None:
        self._by_session: dict[str, list[Event]] = {}
        self._first: dict[str, float] = {}
        self._lock = threading.Lock()

    def append(self, events: Sequence[Event]) -> None:
        with self._lock:
            for e in events:
                self._first.setdefault(e.session, e.at)
                self._by_session.setdefault(e.session, []).append(e)

    def read(self, session: str, *, after: int = 0) -> list[Event]:
        with self._lock:
            return list(self._by_session.get(session, [])[after:])

    def sessions(self) -> list[str]:
        with self._lock:
            return sorted(self._by_session, key=lambda s: (self._first[s], s))

    def delete(self, session: str) -> None:
        with self._lock:
            self._by_session.pop(session, None)
            self._first.pop(session, None)

    def prune(self, before: float) -> int:
        with self._lock:
            old = [s for s, events in self._by_session.items() if max(e.at for e in events) < before]
        for s in old:
            self.delete(s)
        return len(old)


class MemoryLease:
    def __init__(self) -> None:
        self._held: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def acquire(self, name: str, holder: str, ttl_s: float) -> bool:
        now = time.time()
        with self._lock:
            who, until = self._held.get(name, ("", 0.0))
            if who and who != holder and until > now:
                return False
            self._held[name] = (holder, now + ttl_s)
            return True

    def release(self, name: str, holder: str) -> None:
        with self._lock:
            if self._held.get(name, ("",))[0] == holder:
                del self._held[name]

    def holder(self, name: str) -> str:
        with self._lock:
            who, until = self._held.get(name, ("", 0.0))
            return who if until > time.time() else ""
