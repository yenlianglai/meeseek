#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: add meeseek's workstream context to a
prompt about a piece of work.

The prompt names a ticket or an MR, or the session runs on a branch whose name
holds a ticket key: meeseek's notes on that work (goal, state, decisions, open
questions, latest events) go in as context, and the session is attached to
the workstream, so meeseek knows which Claude session works on it. The same
notes are added again only after they change. meeseek not running: nothing
happens, and the prompt goes on as typed.

Install (user level, every project) in ~/.claude/settings.json:

    {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command",
        "command": "python3 /path/to/meeseek/scripts/workstream_hook.py"}]}]}}

MEESEEK_URL overrides the server (default http://127.0.0.1:8787). Standard
library only: it runs with any python3.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SERVER = os.environ.get("MEESEEK_URL", "http://127.0.0.1:8787").rstrip("/")
SEEN = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "meeseek-workstream-hook"


def _branch(cwd: str) -> str:
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ""
    branch = out.stdout.strip() if out.returncode == 0 else ""
    return "" if branch in ("HEAD", "main", "master", "develop") else branch


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()   # hash() differs per process


def _seen(session: str) -> dict:
    try:
        return json.loads((SEEN / f"{session}.json").read_text())
    except (OSError, ValueError):
        return {}


def _remember(session: str, ws_id: str, context: str) -> None:
    try:
        SEEN.mkdir(parents=True, exist_ok=True)
        seen = _seen(session)
        seen[ws_id] = _digest(context)
        (SEEN / f"{session}.json").write_text(json.dumps(seen))
    except OSError:
        pass


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    session, cwd = event.get("session_id", ""), event.get("cwd") or os.getcwd()
    query = urllib.parse.urlencode({"text": (event.get("prompt") or "")[:4000], "branch": _branch(cwd),
                                    "claude_session": session, "cwd": cwd})
    try:
        with urllib.request.urlopen(f"{SERVER}/api/workstreams/lookup?{query}", timeout=2) as r:
            ws = json.loads(r.read() or b"{}")
    except (OSError, ValueError):
        return 0                                   # meeseek is not up: the prompt goes on as typed
    context = ws.get("context", "")
    if not context or _seen(session).get(ws["id"]) == _digest(context):
        return 0
    _remember(session, ws["id"], context)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
