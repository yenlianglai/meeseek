"""Every {{placeholder}} a skill names is filled: by the core, or by a pack in packs/."""

import importlib
import pkgutil
import re

from meeseek.config import home
from meeseek.context.prompts import _get_placeholder_values

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def _pack_placeholders() -> set[str]:
    names: set[str] = set()
    for mod in pkgutil.iter_modules():
        if mod.name.startswith("meeseek_pack_") and mod.name != "meeseek_pack_kit":
            pack = importlib.import_module(mod.name)
            if hasattr(pack, "placeholders"):
                names |= set(pack.placeholders())
    return names


def test_every_skill_placeholder_is_filled():
    defined = set(_get_placeholder_values()) | _pack_placeholders()
    used: dict[str, set[str]] = {}
    for f in home().skills.glob("*/SKILL.md"):
        for key in _PLACEHOLDER.findall(f.read_text(encoding="utf-8")):
            used.setdefault(key, set()).add(f.parent.name)
    undefined = {k: v for k, v in used.items() if k not in defined}
    assert not undefined, f"undefined placeholders: {undefined}"
