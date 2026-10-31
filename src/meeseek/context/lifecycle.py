"""One lifecycle for what meeseek learns on its own, so nothing piles up.

Every learned item — an edge of the team graph (context/ontology.py), a fact
about you (context/personal.py) — is in one of three states:

    confirmed   a person wrote it down (the roster, about_me.md): kept until
                they take it out
    pattern     seen in SURE separate sources: kept while it keeps turning
                up, retired after STALE_DAYS without being seen again
    guess       seen once: retired after GUESS_DAYS unless seen again

`expired` is the rule both stores apply each time they are saved and in the
nightly tidy, so a store holds what is current without anyone pruning it.
Items are the same item when `same` says so — the character-bigram
containment learned rules use (runtime/learned_rules.py) — and merge rather
than pile up as rewordings. Consented rules and remembered facts have their
own windows (learned_rules.MAX_RULES and the weekly review; memory.py's
FACTS_WINDOW_DAYS); what goes into a turn is held to a budget per section
(memory.py).
"""

from __future__ import annotations

import re
import time
from datetime import datetime

SURE = 2             # sources before a guess is a pattern
GUESS_DAYS = 30      # a guess not seen again in this long is retired
STALE_DAYS = 180     # a pattern not seen in this long is retired
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def status(*, confirmed: bool, seen: int) -> str:
    """`confirmed`, `pattern` or `guess`."""
    if confirmed:
        return "confirmed"
    return "pattern" if seen >= SURE else "guess"


def expired(*, confirmed: bool, seen: int, last_seen: float, now: float | None = None) -> bool:
    """Whether a learned item has run out its time (see the module's states)."""
    if confirmed or not last_seen:
        return False
    days = ((now or time.time()) - last_seen) / 86400
    return days > (STALE_DAYS if seen >= SURE else GUESS_DAYS)


def last_seen_of(sources: list[str], stamped: float = 0) -> float:
    """When an item was last seen: its stamp, or else the latest date named in
    its sources ("Data team daily 2026-10-12") — for items written before
    they were stamped."""
    dates = [datetime.fromisoformat(m.group(1)).timestamp()
             for s in sources if (m := _DATE.search(s))]
    return max([stamped, *dates]) if (stamped or dates) else 0


def same(a: str, b: str) -> bool:
    """Two learned items say the same thing (a rewording, not a new item)."""
    from meeseek.runtime.learned_rules import SIMILAR, similarity

    return similarity(a, b) >= SIMILAR
