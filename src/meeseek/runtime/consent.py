"""A learned rule or a remembered fact is written only after the user agreed.

The prompts say so too, but compliance swings on trivial request changes
(flash-lite: 0/10 to 10/10 on one eval case), so this plugin enforces it. A
write goes through when the user asked for it this turn ("寫進規則", "記住…"),
when an `ask_user` from this turn or the one before got a non-refusal answer,
or when it was approved on the Inbox page (runtime/inbox.py). Otherwise the
call gets back what to do instead, and nothing is written.
"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)

# Writes that change every later turn: they wait for a yes.
CONSENT_TOOLS = {"propose_sop_rule": "這條規則", "remember_fact": "這筆記憶",
                 "forget_sop_rule": "移除這條規則"}



