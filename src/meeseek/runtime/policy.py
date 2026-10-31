"""The policy: what the harness tells the agents, and which version of it ran.

meeseek learns at the level of its harness, not the model (see
runtime/improve.py): the SKILL.md bodies, the router and common prompts, the
learned rules, and the read tools' and skills' descriptions. This module names
a version of that text. `manifest()` hashes each component as it stands on
disk; `bundle_id()` names the whole. Every turn records them
(runtime/trajectory.py), so a change in how well a skill does can be traced to
the change in its text that came before it.

A `PolicyBundle` is a policy other than the one on disk, as overrides on it;
`using(bundle)` runs a replay under it (runtime/replays.py) — the weekly run
probes candidate edits this way, in the one app, without touching what a
person's turn runs (runtime/improve.py). `reload` takes an edited policy on disk
live without a restart.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import itertools
import subprocess
from contextvars import ContextVar
from dataclasses import dataclass, field

from meeseek.config import PROJECT_ROOT


def _sha8(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


@functools.cache
def manifest() -> dict[str, str]:
    """component → hash of its text: each skill's body and description, the
    router, the common block, the memory block's headings, and git HEAD."""
    from meeseek.context import prompts
    from meeseek.runtime.skills import discover_skills

    out: dict[str, str] = {}
    for s in discover_skills():
        out[f"skill:{s.name}"] = _sha8(s.body)
        out[f"desc:{s.name}"] = _sha8(s.description)
    for name in ("agents/router", "blocks/common", "blocks/memory"):
        try:
            out[f"prompt:{name}"] = _sha8(prompts.source(name))
        except Exception:  # noqa: BLE001 — a prompt missing is a provenance gap, not a failed turn
            out[f"prompt:{name}"] = "missing"
    out["head"] = _head()
    return out


def _head() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


@functools.cache
def bundle_id() -> str:
    """The version of the whole policy: the hash of the manifest, git HEAD aside
    (a commit elsewhere is not a policy change)."""
    m = {k: v for k, v in manifest().items() if k != "head"}
    return _sha8(repr(sorted(m.items())))


def components_of(agents: list[str]) -> dict[str, str]:
    """The manifest entries of the agents that answered a turn ("sprint_agent"
    → skill:sprint and desc:sprint), plus the router's and common's."""
    m = manifest()
    names = {a.removesuffix("_agent") for a in agents}
    keep = {f"skill:{n}" for n in names} | {f"desc:{n}" for n in names} | {"prompt:agents/router", "prompt:blocks/common"}
    return {k: v for k, v in m.items() if k in keep}


# --- variants: a policy other than the one on disk, for a replay -----------------------------

# What a proposal may change (runtime/improve.py), and so what a variant may
# override. Never the grader (judge prompts, rubrics), never the safety rails
# (consent, harness, learned-rule writes), never frontmatter — `tools:` there is
# a write grant (tests/unit/test_security_policy.py) — and never a tool that
# acts on the world: how a write tool is described moves how often the model
# asks first.
EDITABLE_PROMPTS = ("agents/router", "blocks/common")


@dataclass(frozen=True)
class PolicyBundle:
    """A policy as overrides on the one on disk: SKILL.md bodies, skills'
    routing descriptions, whole prompts or sections, a skill's learned rules
    (raw bullet lines), read tools' descriptions."""
    id: str
    skills: dict = field(default_factory=dict)
    descriptions: dict = field(default_factory=dict)
    prompts: dict = field(default_factory=dict)
    rules: dict = field(default_factory=dict)
    tool_descriptions: dict = field(default_factory=dict)


_ACTIVE: ContextVar[PolicyBundle | None] = ContextVar("policy_bundle", default=None)
_generation = itertools.count(1)
_GEN = [0]


def overlay(*, skills=None, descriptions=None, prompts=None, rules=None, tool_descriptions=None) -> PolicyBundle:
    """A bundle overriding what it names; the rest is the policy on disk."""
    parts = {"skills": dict(skills or {}), "descriptions": dict(descriptions or {}), "prompts": dict(prompts or {}),
             "rules": {k: tuple(v) for k, v in (rules or {}).items()},
             "tool_descriptions": dict(tool_descriptions or {})}
    return PolicyBundle(id=_sha8(repr(sorted((k, sorted(v.items())) for k, v in parts.items()))), **parts)


@contextlib.contextmanager
def using(bundle: PolicyBundle | None):
    """Run what is inside under `bundle`. Only replays set one (runtime/replays.py):
    a person's turn always runs the policy on disk."""
    token = _ACTIVE.set(bundle)
    try:
        yield bundle
    finally:
        _ACTIVE.reset(token)


def active() -> PolicyBundle | None:
    return _ACTIVE.get()


def generation() -> int:
    """Bumped by `reload`: what an agent memoized from disk is stale after it."""
    return _GEN[0]


def editable(target: str) -> bool:
    """Whether a proposal may change `target` ("skill:sprint", "desc:sprint",
    "rules:sprint", "prompt:agents/router", "tool:kb_grep")."""
    from meeseek.runtime.harness import has_effect

    kind, _, name = target.partition(":")
    if kind in ("skill", "desc", "rules"):
        return name in _skill_names()
    if kind == "prompt":
        return name in EDITABLE_PROMPTS
    if kind == "tool":
        from meeseek.tools.registry import GENERAL_TOOLS

        return name in GENERAL_TOOLS and not has_effect(name)
    return False


def _skill_names() -> set[str]:
    from meeseek.runtime.skills import discover_skills

    return {s.name for s in discover_skills()}


def skill_body(name: str, path, fallback: str) -> str:
    """A skill's SOP body under the active bundle, else as it is on disk now."""
    b = active()
    if b and name in b.skills:
        return b.skills[name]
    try:
        from meeseek.runtime.skills import _parse_skill

        return _parse_skill(path.parent if path.name == "SKILL.md" else path).body
    except Exception:  # noqa: BLE001 — a file mid-edit: the body the agent was built with
        return fallback


def reload() -> None:
    """Take the policy on disk as it now stands, without a restart: the hashes
    forgotten, the prompt cache cleared, every agent's instruction rebuilt from
    its SKILL.md on its next call, and the skills' routing descriptions re-read.
    Called after a rule graduates and after an approved proposal is applied."""
    from meeseek.context import prompts

    manifest.cache_clear()
    bundle_id.cache_clear()
    prompts.source.cache_clear()
    _GEN[0] = next(_generation)   # the engine builds its agents for each turn: they read the policy afresh
