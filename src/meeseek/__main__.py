"""meeseek from a terminal, on its own engine.

    meeseek init                                    # a home: project.yaml, .env and the example skills
    meeseek serve                                   # the console, the MCP surface, the scheduler
    meeseek ask "這個 sprint 還剩幾張？"              # a new conversation
    meeseek ask -s cli-1 "那 ACME-17 呢？"            # continue one
    meeseek history cli-1                           # what was said, and done

(`python -m meeseek …` from a checkout is the same.)

Each step prints as it happens: who answers, the tools it calls, what a
policy said. The conversation is kept with the console's, in .cache/meeseek.db.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path


def _show(e) -> None:
    kind = e.kind
    if kind == "model_stepped":
        for c in e.calls:
            args = json.dumps(c.args, ensure_ascii=False)
            print(f"  · {e.by} → {c.name}({args[:160]})", file=sys.stderr)
    elif kind == "tool_held":
        print(f"  ⏸ {e.call.name}: {e.reason[:160]}", file=sys.stderr)
    elif kind == "tool_ran" and e.failed:
        print(f"  ✗ {e.call.name}: {str(e.result)[:160]}", file=sys.stderr)
    elif kind == "policy_spoke":
        print(f"  ⚑ {e.policy}: {e.said[:160]}", file=sys.stderr)


async def _ask(session: str, text: str, watched: bool) -> int:
    from meeseek import engine
    from meeseek.app import build_app

    build_app().install().home.ensure()
    res = await engine.turn(session, text, by="cli", watched=watched, observe=_show)
    print(res.reply)
    if res.held:
        print(f"\n({len(res.held)} write(s) waiting for your yes)", file=sys.stderr)
    return 0


def _history(session: str) -> int:
    from meeseek import engine

    for e in engine.store().read(session):
        if e.kind in ("user_said", "replied"):
            print(f"[{'you' if e.kind == 'user_said' else e.by}] {e.text}\n")
    return 0


def templates() -> Path:
    """project.yaml.example, .env.example and the example skills: the checkout's
    own, or the copies an installed meeseek carries."""
    from meeseek.config.paths import CHECKOUT, PACKAGE_ROOT, PROJECT_ROOT

    return PROJECT_ROOT if CHECKOUT else Path(PACKAGE_ROOT) / "_templates"


def _init(where: str) -> int:
    """Copy what is missing into the home; never overwrite."""
    import shutil

    from meeseek.config.paths import Home, default_root

    root = Path(where).expanduser() if where else default_root()
    src = templates()
    root.mkdir(parents=True, exist_ok=True)
    made = []
    for name, dest in (("project.yaml.example", "project.yaml"), (".env.example", ".env")):
        if not (root / dest).exists():
            shutil.copyfile(src / name, root / dest)
            made.append(dest)
    if not (root / "skills").exists():
        shutil.copytree(src / "skills", root / "skills", ignore=shutil.ignore_patterns("learned.md", "__pycache__"))
        made.append("skills/")
    Home(root=root, skills=root / "skills").ensure()
    print(f"home: {root}" + (f" — made {', '.join(made)}" if made else " — already set up"))
    print("next: fill in project.yaml and .env there, then `meeseek serve` "
          "(MEESEEK_HOME, if it is not the default; README → Setup)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["serve"]:
        from meeseek.server import main as serve

        return serve(argv[1:])
    parser = argparse.ArgumentParser(prog="meeseek")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the server (meeseek serve --help)")
    init = sub.add_parser("init", help="make a home: project.yaml, .env and the example skills")
    init.add_argument("--home", default="", help="where (default: MEESEEK_HOME, else ~/.meeseek)")
    ask = sub.add_parser("ask", help="run one turn")
    ask.add_argument("text")
    ask.add_argument("-s", "--session", default="", help="continue this conversation")
    ask.add_argument("--unwatched", action="store_true", help="as if nobody were watching: writes wait, a clock runs")
    hist = sub.add_parser("history", help="print a conversation")
    hist.add_argument("session")
    args = parser.parse_args(argv)
    if args.cmd == "init":
        return _init(args.home)
    if args.cmd == "ask":
        session = args.session or f"cli-{uuid.uuid4().hex[:8]}"
        if not args.session:
            print(f"(session {session})", file=sys.stderr)
        return asyncio.run(_ask(session, args.text, watched=not args.unwatched))
    return _history(args.session)


if __name__ == "__main__":
    sys.exit(main())
