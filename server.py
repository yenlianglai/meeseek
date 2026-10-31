"""Run meeseek's server from a checkout — the same as `meeseek serve`:

    uv run python server.py [--host 127.0.0.1] [--port 8787] [--no-scheduler] [--reload]
"""

from meeseek.server import main

if __name__ == "__main__":
    raise SystemExit(main())
