"""Before the open-source release: nothing in the tracked tree may name what the
release must not carry — people, internal systems, a team's data, someone else's
characters.

    uv run python scripts/oss_check.py            # exit 1 and every hit on a finding

The patterns live in `.oss-forbidden` (one regex per line, `#` comments), which
git ignores: the list itself names those people and things. Without the file
the check has nothing to look for and says so. File names are checked as well
as contents; binary files by name only. What `.oss-internal` names (tracked:
the team's own modules, such as the animation) stays on GitLab, is not
released, and is not checked.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIST = ROOT / ".oss-forbidden"
INTERNAL = ROOT / ".oss-internal"


def internal(path: Path = INTERNAL) -> tuple[str, ...]:
    """The path prefixes kept out of the release."""
    if not path.exists():
        return ()
    lines = (ln.strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return tuple(ln for ln in lines if ln and not ln.startswith("#"))


def patterns(path: Path = LIST) -> list[re.Pattern[str]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(re.compile(line))
    return out


def tracked(root: Path = ROOT) -> list[str]:
    """What the release carries: the tracked files, less what `.oss-internal` keeps back."""
    res = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True)
    kept = internal(root / INTERNAL.name)
    return [f for f in res.stdout.split("\0") if f and not f.startswith(kept)]


def findings(files: list[str], pats: list[re.Pattern[str]], root: Path = ROOT) -> list[str]:
    hits = []
    for f in files:
        for p in pats:
            if p.search(f):
                hits.append(f"{f}: file name matches {p.pattern}")
        path = root / f
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for p in pats:
                if p.search(line):
                    hits.append(f"{f}:{n}: {p.pattern}")
    return hits


def main() -> int:
    pats = patterns()
    if not pats:
        print(f"no patterns: put the list in {LIST.name} (one regex per line)")
        return 2
    hits = findings(tracked(), pats)
    for h in hits:
        print(h)
    print(f"{len(hits)} finding(s)" if hits else "clean: nothing the release must not carry")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
