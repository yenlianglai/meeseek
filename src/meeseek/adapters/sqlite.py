"""SQLite: the event store and the scheduler's lease, in one file.

One file is the whole of it — copy it and the history moves with you. The
lease is a row with an expiry, renewed by its holder: a lock that holds across
processes and containers alike, which flock on a Docker Desktop file share
does not.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Sequence

from meeseek.core.events import Event, dump, load_one

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    session TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    turn    TEXT NOT NULL,
    at      REAL NOT NULL,
    body    TEXT NOT NULL,
    PRIMARY KEY (session, seq)
);
CREATE TABLE IF NOT EXISTS leases (
    name    TEXT PRIMARY KEY,
    holder  TEXT NOT NULL,
    until   REAL NOT NULL
);
"""


class _Db:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


class SqliteEventStore(_Db):
    def append(self, events: Sequence[Event]) -> None:
        if not events:
            return
        rows = dump(list(events))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                next_seq: dict[str, int] = {}
                for e, raw in zip(events, rows):
                    if e.session not in next_seq:
                        (top,) = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE session = ?",
                                              (e.session,)).fetchone()
                        next_seq[e.session] = top
                    next_seq[e.session] += 1
                    conn.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                                 (e.session, next_seq[e.session], e.kind, e.turn, e.at,
                                  json.dumps(raw, ensure_ascii=False)))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def read(self, session: str, *, after: int = 0) -> list[Event]:
        with self._connect() as conn:
            rows = conn.execute("SELECT body FROM events WHERE session = ? AND seq > ? ORDER BY seq",
                                (session, after)).fetchall()
        return [load_one(json.loads(body)) for (body,) in rows]

    def sessions(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT session FROM events GROUP BY session ORDER BY MIN(at), session").fetchall()
        return [s for (s,) in rows]

    def delete(self, session: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM events WHERE session = ?", (session,))

    def prune(self, before: float) -> int:
        """Drop the conversations whose last event is before `before`; how many."""
        with self._lock, self._connect() as conn:
            old = [s for (s,) in conn.execute("SELECT session FROM events GROUP BY session HAVING MAX(at) < ?",
                                              (before,))]
            conn.executemany("DELETE FROM events WHERE session = ?", [(s,) for s in old])
        return len(old)


class SqliteLease(_Db):
    def acquire(self, name: str, holder: str, ttl_s: float) -> bool:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT holder, until FROM leases WHERE name = ?", (name,)).fetchone()
                if row and row[0] != holder and row[1] > now:
                    conn.execute("COMMIT")
                    return False
                conn.execute("INSERT INTO leases VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE "
                             "SET holder = excluded.holder, until = excluded.until", (name, holder, now + ttl_s))
                conn.execute("COMMIT")
                return True
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def release(self, name: str, holder: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM leases WHERE name = ? AND holder = ?", (name, holder))

    def holder(self, name: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT holder, until FROM leases WHERE name = ?", (name,)).fetchone()
        return row[0] if row and row[1] > time.time() else ""
