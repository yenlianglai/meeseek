"""Whether a turn is a replay or an eval run rather than a person's.

A replayed or evaluated turn runs as one of REPLAY_USERS, or under
`replay_scope()`: never a turn of yours, so never on the scoreboard and never
a signal about a reply. Both engines ask.
"""

from __future__ import annotations

from contextvars import ContextVar

REPLAY_USERS = frozenset({"replay", "eval"})
_REPLAYING: ContextVar[bool] = ContextVar("replaying", default=False)


def replaying(user_id: str = "") -> bool:
    return _REPLAYING.get() or user_id in REPLAY_USERS
