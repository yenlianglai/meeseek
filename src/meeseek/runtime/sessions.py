"""The names sessions go by, and the ADK-era session store.

Conversations are kept in the engine's event store (engine.store()). The
sessions.db ADK wrote before meeseek ran its own engine is read through
runtime/history.py and pruned here; nothing writes it any more.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from meeseek.config.paths import home

# The app name in the console's session URLs (/apps/<APP_NAME>/users/…), and on
# every row of the ADK-era sessions.db. The console repeats it as a TypeScript
# constant (frontend/src/lib/api.ts), which a test holds against this one.
APP_NAME = "agents"

# Who owns a session. The console's turns are "webui" (frontend/src/lib/api.ts);
# headless runs (plan-my-day) and MCP callers get their own users so they never
# surface in the console's session list.
WORKFLOW_USER = "workflow"




@contextmanager
def read_only_connection(path: Path | None = None):
    """A read-only sqlite connection, or None when the database does not exist
    yet (a fresh checkout) or cannot be opened — callers render an empty list
    rather than 500."""
    db = Path(path or home().sessions_db)
    if not db.exists():
        yield None
        return
    try:
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        yield None
        return
    try:
        yield conn
    finally:
        conn.close()


def prune(days: int, path: Path | None = None, now: float | None = None) -> int:
    """Drop conversations not touched in `days` — their events and the session
    rows — so the store keeps what the past-sessions tools can read
    (runtime/past_sessions.py MAX_DAYS) and no more; how many sessions went.
    The pages freed are reused by later writes."""
    import time

    db = Path(path or home().sessions_db)
    if not db.exists() or days <= 0:
        return 0
    cutoff = (now or time.time()) - days * 86400
    conn = sqlite3.connect(db, timeout=30)
    try:
        old = [r[0:3] for r in conn.execute(
            "SELECT app_name, user_id, id FROM sessions WHERE update_time < ?", (cutoff,))]
        with conn:
            for app, user, sid in old:
                conn.execute("DELETE FROM events WHERE app_name = ? AND user_id = ? AND session_id = ?",
                             (app, user, sid))
                conn.execute("DELETE FROM sessions WHERE app_name = ? AND user_id = ? AND id = ?", (app, user, sid))
        return len(old)
    finally:
        conn.close()
