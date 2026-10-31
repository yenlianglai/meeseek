"""What a tool is: a function, its schema, and what it does to the world.

    @tool(effect=Effect.WRITE, target=["issue_key"])
    def link_epic(issue_key: str, epic_key: str) -> str:
        \"\"\"Link an issue under an epic.

        Args:
            issue_key: the issue to move
            epic_key: the epic it goes under
        \"\"\"

The schema is read from the type hints and the docstring's Args section, so
the function is the one place it is written. `effect` says whether a call
changes anything outside this process — a write is held for a yes when nobody
is watching, never re-sent blind after an interruption, stubbed in a replay —
and `target` names the arguments that say what it changes.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from pydantic import TypeAdapter


class Effect(str, Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    fn: Callable[..., Any]
    parameters: dict                     # JSON schema of the arguments
    effect: Effect = Effect.READ
    target: tuple[str, ...] = ()         # argument names that identify what a write changes
    shows_raw: bool = False              # the reply must show the result's raw block (logs, a pod table)
    extras: dict = field(default_factory=dict)

    @property
    def writes(self) -> bool:
        return self.effect is Effect.WRITE

    def target_of(self, args: dict) -> dict:
        """The part of a call's arguments that says what it changes."""
        return {k: args[k] for k in self.target if k in args}


_ARGS = re.compile(r"^\s*Args:\s*$", re.M)
_ARG_LINE = re.compile(r"^\s{2,}(\w+)\s*(?:\([^)]*\))?:\s*(.+)$")


def _split_doc(doc: str) -> tuple[str, dict[str, str]]:
    """(description, {arg: description}) from a Google-style docstring."""
    doc = inspect.cleandoc(doc or "")
    m = _ARGS.search(doc)
    if not m:
        return doc.strip(), {}
    head, tail = doc[:m.start()].strip(), doc[m.end():]
    args: dict[str, str] = {}
    current = ""
    for line in tail.splitlines():
        if not line.strip():
            continue
        hit = _ARG_LINE.match(line)
        if hit:
            current = hit.group(1)
            args[current] = hit.group(2).strip()
        elif current and line.startswith("    "):
            args[current] += " " + line.strip()
        else:
            break
    return head, args


def schema_of(fn: Callable[..., Any]) -> dict:
    """The JSON schema of `fn`'s arguments, from its type hints and docstring."""
    sig = inspect.signature(fn)
    try:
        hints = inspect.get_annotations(fn, eval_str=True)
    except Exception:  # noqa: BLE001 — a hint naming a type-checking-only import: read as Any
        hints = {}
    _, docs = _split_doc(fn.__doc__ or "")
    props: dict[str, dict] = {}
    required: list[str] = []
    for name, p in sig.parameters.items():
        if name.startswith("_") or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        prop = TypeAdapter(hints.get(name, Any)).json_schema()
        prop.pop("title", None)
        if name in docs:
            prop["description"] = docs[name]
        if p.default is p.empty:
            required.append(name)
        else:
            prop["default"] = p.default
        props[name] = prop
    schema: dict = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def tool(effect: Effect = Effect.READ, *, target: list[str] | tuple[str, ...] = (), name: str = "",
         shows_raw: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a function a tool; its spec is `fn.spec`."""

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        params = inspect.signature(fn).parameters
        unknown = [t for t in target if t not in params]
        if unknown:
            raise TypeError(f"{fn.__name__}: target names no argument {unknown}")
        if effect is Effect.WRITE and not target:
            raise TypeError(f"{fn.__name__}: a write must say what it changes (target=[...])")
        description, _ = _split_doc(fn.__doc__ or "")
        fn.spec = ToolSpec(name=name or fn.__name__, description=description, fn=fn,  # type: ignore[attr-defined]
                           parameters=schema_of(fn), effect=effect, target=tuple(target), shows_raw=shows_raw)
        return fn

    return wrap
