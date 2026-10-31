"""One model call that is not an agent turn: triage, the judges, meeting notes
and transcripts, the coach, what is learned about you and the team.

    ask(prompt, model=cfg.models.fast)                         → the text
    ask(prompt, model=cfg.models.fast, schema=Triage)          → a Triage
    ask(prompt, model=..., files=[("image/png", data), …])     → the files lead the prompt, in order

It goes through the configured models (adapters/llm.build_model) as every
agent turn does — the gateway when one is set, Gemini natively, else litellm —
so nothing here needs Gemini. What a model must be able to do is the caller's
to know: a transcript needs one that hears audio (`models.multimodal`).
Files are held in memory for the call, never stored. Usage is recorded under
`source`, by default the calling module's name (runtime/usage.py). A
transient failure (a rate limit, a 5xx, a timeout) is asked again once.
"""

from __future__ import annotations

import asyncio
import re
import sys
import uuid
from typing import Any, Sequence

from meeseek.adapters.llm import build_model, hold, release
from meeseek.core.events import Attachment, UserSaid
from meeseek.core.turn import TransientModelError

RETRIES = 1
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


async def aask(prompt: str, *, model: str, system: str = "", schema: Any = None,
               files: Sequence[tuple[str, bytes]] = (), temperature: float | None = None,
               max_output_tokens: int | None = None, timeout_s: float | None = 300, thinking: str | None = None,
               source: str = "") -> Any:
    """The model's answer: text, or an instance of `schema` (a pydantic model)."""
    source = source or "direct"
    refs = [hold(data) for _, data in files]
    try:
        said = UserSaid(session=f"oneshot.{source}", turn=uuid.uuid4().hex[:8], text=prompt,
                        attachments=tuple(Attachment(mime=mime, ref=ref) for (mime, _), ref in zip(files, refs)))
        m = build_model(model, by=source)
        for attempt in range(RETRIES + 1):
            try:
                step = await m.step(system=system, history=[said], tools=[], session=said.session, turn=said.turn,
                                    schema=schema, temperature=temperature, max_output_tokens=max_output_tokens,
                                    timeout_s=timeout_s, thinking=thinking)
                break
            except TransientModelError:
                if attempt == RETRIES:
                    raise
                await asyncio.sleep(2)
    finally:
        for ref in refs:
            release(ref)
    if step.usage is not None:
        from meeseek.runtime.usage import record_usage

        record_usage(source, step.usage)
    text = step.text.strip()
    if schema is None:
        return text
    if not text:
        raise ValueError(f"{source}: the model answered nothing")
    return schema.model_validate_json(_FENCE.sub("", text))


def ask(prompt: str, **kw: Any) -> Any:
    """`aask` from synchronous code; inside a running loop, on a thread of its own."""
    kw.setdefault("source", sys._getframe(1).f_globals.get("__name__", "").rsplit(".", 1)[-1] or "direct")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(aask(prompt, **kw))
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(aask(prompt, **kw))).result()
