"""The open-source repository, as a new history: the tree at HEAD, one commit.

    uv run python scripts/export_oss.py ../meeseek-oss --author "Name <email>"

The branch's history carries what the release must not (people, a team's
data, a borrowed character — removed from the tree, still in old commits), so
the public repository starts from the final tree, not from this one's log.

It refuses unless scripts/oss_check.py finds nothing — with its pattern list
present — and the destination is new or empty. It takes only what git tracks
at HEAD (never project.yaml, .env, the knowledge base or what a skill
learned), less what `.oss-internal` keeps on GitLab (the animation module) and
any .mcp.json server that runs from there; makes one commit by the author you
name, and pushes nowhere: look it over, then add a remote and push it yourself.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import oss_check  # noqa: E402


def _git(*args: str, cwd: Path = ROOT, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, **kw)


def _version() -> str:
    import tomllib

    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def export(dest: Path, author: str, message: str = "") -> str:
    """Write the new repository at `dest`; returns its one commit's id."""
    pats = oss_check.patterns()
    if not pats:
        raise SystemExit(f"refused: no {oss_check.LIST.name} — without it the release check has nothing to look for")
    hits = oss_check.findings(oss_check.tracked(), pats)
    if hits:
        raise SystemExit("refused: scripts/oss_check.py finds\n" + "\n".join(hits))
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"refused: {dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    kept = oss_check.internal()
    archive = _git("archive", "--format=tar", "HEAD").stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        released = [m for m in tar.getmembers() if not (m.name + "/").startswith(kept)]   # a folder has no "/"
        tar.extractall(dest, members=released, filter="data")
    _without_internal_servers(dest / ".mcp.json", kept)
    _git("init", "-q", "-b", "main", cwd=dest)
    _git("add", "-A", cwd=dest)
    _git("-c", "commit.gpgsign=false", "commit", "-q", f"--author={author}", "-m",
         message or f"meeseek {_version()}", cwd=dest,
         env={**os.environ, "GIT_COMMITTER_NAME": author.split("<")[0].strip(),
              "GIT_COMMITTER_EMAIL": author.split("<")[-1].rstrip(">").strip()})
    return _git("rev-parse", "HEAD", cwd=dest, text=True).stdout.strip()


def _without_internal_servers(path: Path, kept: tuple[str, ...]) -> None:
    """Drop the .mcp.json servers that run from a path left out of the release."""
    if not path.exists() or not kept:
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or {}
    data["mcpServers"] = {n: s for n, s in servers.items()
                          if not any(k in json.dumps(s) for k in kept)}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dest", type=Path, help="a new or empty folder")
    ap.add_argument("--author", required=True, help='who the release commit is by: "Name <email>"')
    ap.add_argument("--message", default="", help="the commit message (default: meeseek <version>)")
    args = ap.parse_args(argv)
    if "<" not in args.author or ">" not in args.author:
        ap.error('--author is "Name <email>"')
    commit = export(args.dest.expanduser().resolve(), args.author, args.message)
    print(f"{args.dest}: one commit, {commit[:12]} — nothing pushed. Look it over, then:")
    print(f"  git -C {args.dest} remote add origin <url> && git -C {args.dest} push -u origin main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
