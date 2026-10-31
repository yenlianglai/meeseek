"""How many pictures one turn may make.

When a picture is the right answer is said in the picture tools' own
descriptions (tools/registry.py), so it reaches exactly the agents that hold
them. Headless runs (the morning plan) do not get the tool at all — an image
nobody looks at is pure spend; see registry.read_tools(media=…).
"""

from __future__ import annotations

from meeseek.config import cfg


def media_call_limits() -> dict[str, int]:
    """Per-turn advisory cap for the picture tools.

    callbacks.make_call_cap warns past the limit and only blocks at twice it:
    a storyboard legitimately wants several frames, a loop of variations on
    one prompt should be stopped.
    """
    return {"generate_image": cfg.media.image_calls_per_turn}
