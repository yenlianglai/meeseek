"""Direct Corpus Interaction over the knowledge base: grep/find/read
primitives on the live files, with no index (arXiv:2605.05242).

    kb_grep  — regex/literal match with surrounding context (grep -n -C)
    kb_list  — corpus structure navigation (find / ls)
    kb_read  — windowed file inspection (sed -n 'a,bp')

The searchable corpus is synced sources plus refinement notes. Agent outputs,
the notes injected into every turn (pinned/, facts.md, about_me.md) and the seed examples
are excluded.
"""

from __future__ import annotations

import re
from pathlib import Path

from meeseek.config.paths import home

# One tool result. Sized so a normal 150-line doc window (8-15k chars) fits
# whole and a long-line file (draw.io XML, wide sheets) still yields ~200
# lines per call, while staying well under the 50k callback-level cap.
_MAX_OUTPUT_CHARS = 24_000
_MAX_HITS_PER_FILE = 8  # matches shown per file — enough to answer without a follow-up grep round
_EXCLUDED = (
    ("outputs",),
    ("notes", "pinned"),
    ("notes", "examples"),
    ("notes", "facts.md"),
    ("notes", "about_me.md"),       # in every agent's memory already
    ("notes", "README.md"),
)


def _kb_files() -> list[Path]:
    """Every searchable KB file."""
    if not home().kb.exists():
        return []
    return [
        p for p in sorted(home().kb.rglob("*.md"))
        if not any(p.relative_to(home().kb).parts[:len(ex)] == ex for ex in _EXCLUDED)
    ]


def _compile(pattern: str) -> re.Pattern:
    """Compile as case-insensitive regex; fall back to literal on bad syntax."""
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(pattern), re.IGNORECASE)


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def kb_grep(
    pattern: str,
    also_matching: str = "",
    path_filter: str = "",
    context_lines: int = 2,
    max_files: int = 12,
) -> str:
    """Search the knowledge base for lines matching a regex, with surrounding context.

    Like `grep -n -i -C`. Matches Chinese by substring directly (no tokenization
    needed). Compose constraints instead of writing one long query: search one
    exact term, read the hits, then search again with what you learned.

    Args:
        pattern: Regex or literal to match (case-insensitive), e.g. "rate.?limit" or "角色".
        also_matching: Optional comma-separated extra patterns the FILE must also
            contain somewhere (chained-grep semantics: grep A file | grep-file B).
        path_filter: Optional substring to restrict which file paths are searched
            (e.g. "meetings", "refinements", "prd").
        context_lines: Lines of context around each match (default 2).
        max_files: Max number of files to report (default 12).
    """
    if not pattern:
        return "Error: empty pattern."
    rx = _compile(pattern)
    extra = [_compile(p.strip()) for p in also_matching.split(",") if p.strip()]
    context_lines = _to_int(context_lines, 2)
    max_files = _to_int(max_files, 8)

    files = _kb_files()
    if not files:
        return "Knowledge base is empty (no documents synced yet)."

    sections: list[str] = []
    reported = 0
    total_matched_files = 0
    for path in files:
        rel = path.relative_to(home().kb)
        if path_filter and path_filter.lower() not in str(rel).lower():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if extra and not all(e.search(content) for e in extra):
            continue
        lines = content.splitlines()
        hit_lines = [i for i, line in enumerate(lines) if rx.search(line)]
        if not hit_lines:
            continue
        total_matched_files += 1
        if reported >= max_files:
            continue
        reported += 1

        shown: list[str] = []
        last_end = -1
        for i in hit_lines[:_MAX_HITS_PER_FILE]:
            start = max(0, i - context_lines)
            end = min(len(lines), i + context_lines + 1)
            if start > last_end + 1 and shown:
                shown.append("    ⋮")
            for j in range(max(start, last_end + 1), end):
                marker = ">" if j == i else " "
                shown.append(f"  {j + 1:4d}{marker} {lines[j]}")
            last_end = end - 1
        extra_note = (
            f" (+{len(hit_lines) - _MAX_HITS_PER_FILE} more matches)"
            if len(hit_lines) > _MAX_HITS_PER_FILE
            else ""
        )
        sections.append(f"### {rel} — {len(hit_lines)} match(es){extra_note}\n" + "\n".join(shown))

    if not sections:
        return f"No matches for /{pattern}/. Try a shorter term, a synonym, or kb_list() to see what exists."

    header = f"{total_matched_files} file(s) matched"
    if total_matched_files > reported:
        header += f", showing first {reported} (narrow with path_filter or also_matching)"
    out = header + "\n\n" + "\n\n".join(sections)
    if len(out) > _MAX_OUTPUT_CHARS:
        out = out[:_MAX_OUTPUT_CHARS] + "\n... (truncated — narrow the search)"
    return out


def kb_list(subdir: str = "") -> str:
    """List knowledge base files with size and last-modified date.

    Use this first to understand corpus structure, or to spot recently
    updated documents (results are grouped by directory).

    Args:
        subdir: Optional path substring to filter (e.g. "meetings", "drive").
    """
    files = _kb_files()
    if not files:
        return "Knowledge base is empty (no documents synced yet)."

    from datetime import datetime

    by_dir: dict[str, list[str]] = {}
    shown = 0
    for path in files:
        rel = path.relative_to(home().kb)
        if subdir and subdir.lower() not in str(rel).lower():
            continue
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")
        by_dir.setdefault(str(rel.parent), []).append(
            f"  {rel.name}  ({stat.st_size // 1024}KB, {mtime})"
        )
        shown += 1

    if not by_dir:
        return f"No files match '{subdir}'."
    parts = [f"{shown} file(s):"]
    for d in sorted(by_dir):
        parts.append(f"\n{d}/")
        parts.extend(by_dir[d])
    return "\n".join(parts)


def kb_read(path: str, start_line: int = 1, num_lines: int = 150) -> str:
    """Read a window of a knowledge base file with line numbers.

    Args:
        path: Path relative to knowledge_base/ (as shown by kb_grep/kb_list),
            or a bare filename which will be located automatically.
        start_line: 1-based line to start from (default 1).
        num_lines: How many lines to return (default 150).
    """
    start_line = max(1, _to_int(start_line, 1))
    num_lines = max(1, _to_int(num_lines, 150))

    target = (home().kb / path).resolve()
    if not str(target).startswith(str(home().kb.resolve())):
        return "Error: path escapes the knowledge base."
    if not target.is_file():
        # Bare filename → locate it anywhere in the KB
        name = Path(path).name
        candidates = [p for p in _kb_files() if p.name == name]
        if not candidates:
            candidates = [p for p in _kb_files() if name.lower() in p.name.lower()]
        if not candidates:
            return f"Error: '{path}' not found. Use kb_list() or kb_grep() to locate files."
        if len(candidates) > 1:
            opts = "\n".join(f"- {p.relative_to(home().kb)}" for p in candidates[:10])
            return f"Ambiguous filename '{path}' — candidates:\n{opts}"
        target = candidates[0]

    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return f"Error reading {path}: {e}"

    total = len(lines)
    rel = target.relative_to(home().kb)
    if start_line > total:
        return f"Error: {rel} has {total} lines; start_line={start_line} is past the end."

    # Fill the window by character budget, whole lines only, so the footer —
    # and its "continue at line N" hint — always survives.
    budget = _MAX_OUTPUT_CHARS - 300  # room for footer / notes
    numbered: list[str] = []
    used = 0
    clipped_line = False
    for i, line in enumerate(lines[start_line - 1 : start_line - 1 + num_lines]):
        entry = f"{start_line + i:4d}  {line}"
        if used + len(entry) + 1 > budget:
            if not numbered:  # a single line bigger than the whole budget
                entry = entry[:budget] + " …(line clipped)"
                numbered.append(entry)
                clipped_line = True
            break
        numbered.append(entry)
        used += len(entry) + 1

    body = "\n".join(numbered)
    end = start_line + len(numbered) - 1
    footer = f"\n\n[{rel} — lines {start_line}-{end} of {total}]"
    requested_end = min(total, start_line + num_lines - 1)
    if end < requested_end and not clipped_line:
        footer += f" (stopped early: output limit {_MAX_OUTPUT_CHARS:,} chars)"
    if end < total:
        footer += f" Continue with kb_read('{rel}', start_line={end + 1})."
    return body + footer
