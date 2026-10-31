"""Prompt templates: the engine's prompt files and `{{placeholder}}` rendering.

Every engine-owned text a model receives lives under `prompts/` (manifest:
prompts/README.md) and is read by name through `text()`. `render()` fills
`{{placeholders}}` from project.yaml for those files and for skill SOPs alike.
"""

from __future__ import annotations

import functools
import re

from meeseek.config import PROMPTS_DIR, cfg


def _get_placeholder_values() -> dict[str, str]:
    """Build the dict of placeholder values from meeseek.config."""
    teams = cfg.project.teams or ([cfg.project.team_name] if cfg.project.team_name else [])
    teams_list = ", ".join(f"[{t}]" for t in teams) if teams else "[participant]"

    return {
        "team_name": cfg.project.team_name,
        "teams": teams_list,
        "default_gcp_project": cfg.google_cloud.quota_project,
    }


def _pack_placeholders() -> dict[str, str]:
    """Values the running packs offer (an issue tracker's project key, …)."""
    from meeseek.app import current

    a = current()
    return a.mcp_tools.placeholders() if a is not None and a.mcp_tools is not None else {}


def render(template: str, extra_vars: dict[str, str] | None = None) -> str:
    """Render a template string, replacing {{placeholders}} from meeseek.config.

    Used for prompt files and skill SOPs/descriptions alike. Unknown
    placeholders are left untouched.
    """
    placeholders = {**_pack_placeholders(), **_get_placeholder_values()}
    if extra_vars:
        placeholders.update(extra_vars)

    def _replace(match: re.Match) -> str:
        key = match.group(1)
        return placeholders.get(key, match.group(0))

    return re.sub(r"\{\{(\w+)\}\}", _replace, template)


# A section heading: `## name` alone on a line, name in lowercase ASCII. Any
# other heading (`## 回覆風格`, `## Coding Conventions`) is prompt content.
_SECTION = re.compile(r"^## ([a-z][a-z0-9_]*)[ \t]*$", re.MULTILINE)
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def sections(raw: str) -> dict[str, str]:
    """The `## name` sections of a prompt file, each without its surrounding
    newlines. Text before the first section is a note for the reader."""
    heads = list(_SECTION.finditer(raw))
    out: dict[str, str] = {}
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(raw)
        if m.group(1) in out:
            raise ValueError(f"duplicate prompt section: {m.group(1)}")
        out[m.group(1)] = raw[m.end():end].strip("\n")
    return out


def source(name: str) -> str:
    """The unrendered text of a prompt.

    `name` is a path under prompts/ without `.md` ("agents/router"), which
    yields the whole file byte for byte, or that plus `#section`
    ("runtime/callbacks#grace"), which yields one section. Under a replay's
    policy bundle (runtime/policy.py), its version of the prompt.
    """
    from meeseek.runtime.policy import active

    b = active()
    if b is not None and name in b.prompts:
        return b.prompts[name]
    return _source_disk(name)


@functools.cache
def _source_disk(name: str) -> str:
    file, _, section = name.partition("#")
    raw = (PROMPTS_DIR / f"{file}.md").read_text(encoding="utf-8")
    if not section:
        return raw
    found = sections(raw)
    if section not in found:
        raise KeyError(f"prompts/{file}.md has no section '## {section}'")
    return found[section]


source.cache_clear = _source_disk.cache_clear   # type: ignore[attr-defined]  # policy.reload clears it


def text(name: str, **values: object) -> str:
    """A prompt by name, rendered: project.yaml placeholders plus `values`.

    A placeholder that neither supplies raises, so a prompt cannot reach a
    model with a `{{hole}}` in it. Values are inserted as-is: braces inside
    them are never rendered.
    """
    template = source(name)
    extra = {k: str(v) for k, v in values.items()}
    known = set(_pack_placeholders()) | set(_get_placeholder_values()) | set(extra)
    missing = sorted({k for k in _PLACEHOLDER.findall(template) if k not in known})
    if missing:
        raise KeyError(f"prompt {name!r} needs values for: {', '.join(missing)}")
    return render(template, extra)
