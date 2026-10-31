"""Reading and searching code at any ref, from the host-side git mirror.

A search is `git grep` over the exact commit under review, with context; a
read carries line numbers and can take several windows at once. GitLab's blob
search (default branch only, short snippets) and file API (no line numbers)
cost the model a step per window instead.
"""

from __future__ import annotations

import difflib
import re
import subprocess

_MAX_SEARCH_CHARS = 12_000
_PER_FILE_MATCHES = 20


def _commit(project: str, ref: str):
    """(project, Ref, mirror path) for a ref of an allowlisted repo."""
    from meeseek_pack_gitlab import git_mirror
    from meeseek_pack_gitlab.gitlab import split_ref

    project, ref = split_ref(project, ref)
    r = git_mirror.resolve(project, ref)
    return project, r, git_mirror.ensure(r)


def search_code(query: str, project: str = "", ref: str = "", path: str = "",
                context: int = 2) -> str:
    """`git grep` at one commit: every match with its line number and context."""
    from meeseek_pack_gitlab.git_mirror import _git

    try:
        project, r, repo = _commit(project, ref)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    ctx = max(0, min(int(context or 0), 10))
    args = ["grep", "-n", "-I", "--heading", "--break", f"--max-count={_PER_FILE_MATCHES}",
            f"-C{ctx}"]
    if query == query.lower():
        args.append("-i")  # smart case: lower-case means "any case"
    pathspec = ["--", path] if path else []
    proc = _git(repo, *args, "-E", "-e", query, r.sha, *pathspec)
    if proc.returncode == 128:  # not a valid regex — search it literally
        proc = _git(repo, *args, "-F", "-e", query, r.sha, *pathspec)
    where = f"{project} @ {r.desc} {r.sha[:8]}" + (f", path {path}" if path else "")
    if proc.returncode == 1:
        return f"No matches for `{query}` in {where}."
    if proc.returncode != 0:
        return f"Error: git grep failed: {proc.stderr.decode(errors='replace')[:300]}"

    out, files, matches, used, cut = [], 0, 0, 0, False
    for line in proc.stdout.decode(errors="replace").splitlines():
        if line.startswith(f"{r.sha}:"):
            line = f"### {line[len(r.sha) + 1:]}"
            files += 1
        elif re.match(r"^\d+:", line):
            matches += 1
        if used + len(line) > _MAX_SEARCH_CHARS:
            cut = True
            break
        out.append(line)
        used += len(line) + 1
    head = f"**{matches} match(es) in {files} file(s)** for `{query}` in {where}"
    if cut:
        head += " — output cut; narrow it with `path` (e.g. 'app/core/') or a more specific pattern"
    return head + "\n" + "\n".join(out)


def _parse_ranges(ranges: str) -> list[tuple[int, int]]:
    out = []
    for part in re.split(r"[,\s]+", ranges.strip()):
        if m := re.fullmatch(r"(\d+)(?:-(\d+))?", part):
            a = int(m.group(1))
            b = int(m.group(2) or a)
            out.append((min(a, b), max(a, b)))
        elif part:
            raise ValueError(f"'{part}' is not a line range like 120-180")
    return out


def _closest_paths(repo, sha: str, file_path: str) -> list[str]:
    from meeseek_pack_gitlab.git_mirror import _git

    names = _git(repo, "ls-tree", "-r", "--name-only", sha).stdout.decode(errors="replace").splitlines()
    tail = [n for n in names if n.endswith("/" + file_path)]
    base = file_path.rsplit("/", 1)[-1]
    same_name = [n for n in names if n.rsplit("/", 1)[-1] == base]
    return (tail or same_name)[:5] or difflib.get_close_matches(file_path, names, n=5, cutoff=0.5)


def get_file(file_path: str, ref: str = "", project: str = "", start_line: int = 1,
             num_lines: int = 0, ranges: str = "") -> str:
    """A file at a ref, with line numbers: one window, or several via `ranges`."""
    from meeseek_pack_gitlab import git_mirror
    from meeseek_pack_gitlab.gitlab import _MAX_FILE_CHARS, _file_window, gitlab_get_file

    path = file_path.strip().lstrip("/")
    try:
        project, r, repo = _commit(project, ref)
    except ValueError as e:
        return f"Error: {e}"
    except (RuntimeError, subprocess.SubprocessError):
        # The mirror could not fetch (git missing, network); the API still can.
        return gitlab_get_file(path, ref or "main", project=project,
                               start_line=start_line, num_lines=num_lines)
    data = git_mirror.read(repo, r.sha, path)
    at = f"{r.desc} {r.sha[:8]}"
    if data is None:
        near = _closest_paths(repo, r.sha, path)
        hint = ("Did you mean: " + ", ".join(f"`{n}`" for n in near)) if near else \
            "Use search_code to find where it lives."
        return f"Error: `{path}` does not exist in {project} @ {at}. {hint}"
    content = data.decode("utf-8", errors="replace")
    if not ranges:
        return _file_window(content, path, path, ref or r.desc.split()[-1], project, len(data),
                            start_line, num_lines, numbered=True)
    try:
        windows = _parse_ranges(ranges)
    except ValueError as e:
        return f"Error: {e}"
    lines = content.splitlines()
    blocks, used = [], 0
    for a, b in windows:
        body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(a, min(b, len(lines)) + 1))
        if used + len(body) > _MAX_FILE_CHARS - 400:
            blocks.append(f"(lines {a}-{b} left out: the result is full — read them in another call)")
            continue
        blocks.append(f"lines {a}-{min(b, len(lines))}:\n```\n{body}\n```")
        used += len(body)
    return f"**`{path}`** @ {at} ({len(lines)} lines)\n" + "\n".join(blocks)
