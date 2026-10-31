"""Models for meeseek's own turn runner (core/turn.py): one step each.

    OpenAIModel   any OpenAI-compatible endpoint: a gateway (GoModel, a litellm
                  proxy), OpenAI itself, a local server
    LitellmModel  any provider litellm knows, in process
    GenaiModel    Gemini through google-genai (Vertex or the Gemini API)

All three read the conversation as core messages and answer with a
ModelStepped. A failure worth asking again (a rate limit, a 5xx, a timeout,
a dropped connection) is raised as TransientModelError, which the runner
retries; a request refused as longer than the context window as
ContextOverflowError, which the runner answers by trimming; anything else
ends the turn. A response that hit its output limit says so (`cut_off`).
`build_model` picks one by name.

`step` also takes what a one-shot call asks for (runtime/oneshot.py), each
optional: `schema` (a pydantic model the answer is JSON for), `temperature`,
`max_output_tokens`, `timeout_s`, and `thinking` (Gemini's thinking level;
other models ignore it).
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any, Callable, Sequence

from meeseek.core.events import Event, ModelStepped, ToolCall, Usage
from meeseek.core.tools import ToolSpec
from meeseek.core.turn import ContextOverflowError, Message, TransientModelError, conversation

# Gemini wants a thought signature on every function call it is shown; a call
# it never made (none recorded) is let through with the documented stand-in.
SKIP_SIGNATURE = "skip_thought_signature_validator"


# --- the OpenAI chat format ------------------------------------------------------

# Files a one-shot call sends without keeping (an audio window, a screenshot):
# held here, by a `mem:` ref, only for the call.
_INLINE: dict[str, bytes] = {}


def hold(data: bytes) -> str:
    """A ref the adapters can load `data` by, until `release(ref)`."""
    ref = f"mem:{uuid.uuid4().hex}"
    _INLINE[ref] = data
    return ref


def release(ref: str) -> None:
    _INLINE.pop(ref, None)


def _load(ref: str) -> bytes:
    if ref in _INLINE:
        return _INLINE[ref]
    from meeseek.adapters.blobs import blobs

    return blobs().get(ref)


# How each provider says a request is longer than the model takes.
_OVERFLOW = re.compile(r"context.?length|context.?window|maximum context|too many (input )?tokens|"
                       r"input token count|exceeds the maximum number of tokens|prompt is too long|"
                       r"request too large|input is too long", re.IGNORECASE)


def _overflow(e: Exception) -> bool:
    return "ContextWindowExceeded" in type(e).__name__ or bool(_OVERFLOW.search(str(e)))


def _json_format(schema: Any) -> dict:
    """OpenAI's response_format for a pydantic model."""
    return {"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}}


def _data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def openai_user_content(m: Message, load: Callable[[str], bytes] = _load) -> str | list[dict]:
    """A user message's text, with its files leading as content parts."""
    if not m.attachments:
        return m.text
    parts: list[dict] = []
    for a in m.attachments:
        uri = _data_uri(a.mime, load(a.ref))
        if a.mime.startswith("image/"):
            parts.append({"type": "image_url", "image_url": {"url": uri}})
        else:
            parts.append({"type": "file", "file": {"filename": f"attachment.{a.mime.rsplit('/', 1)[-1]}",
                                                   "file_data": uri}})
    if m.text:
        parts.append({"type": "text", "text": m.text})
    return parts


def openai_messages(system: str, messages: Sequence[Message], *, google: bool = False,
                    load: Callable[[str], bytes] = _load) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        if m.role == "assistant":
            om: dict[str, Any] = {"role": "assistant", "content": m.text if (m.text or not m.calls) else None}
            if m.calls:
                om["tool_calls"] = []
                for c in m.calls:
                    tc: dict[str, Any] = {"id": c.id, "type": "function",
                                          "function": {"name": c.name, "arguments": json.dumps(c.args, ensure_ascii=False)}}
                    extra = c.meta.get("extra_content")
                    if extra is None and google:
                        extra = {"google": {"thought_signature": c.meta.get("thought_signature") or SKIP_SIGNATURE}}
                    if extra is not None:
                        tc["extra_content"] = extra
                    om["tool_calls"].append(tc)
            out.append(om)
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.call_id, "name": m.name, "content": m.text})
        else:
            out.append({"role": "user", "content": openai_user_content(m, load)})
    return out


def openai_tools(tools: Sequence[ToolSpec]) -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": t.parameters or {"type": "object", "properties": {}}}}
            for t in tools]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def from_openai(resp: Any, *, session: str, turn: str, by: str) -> ModelStepped:
    choice = _get(resp, "choices")[0]
    msg = _get(choice, "message")
    calls = []
    for tc in _get(msg, "tool_calls") or []:
        fn = _get(tc, "function")
        raw = _get(fn, "arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError:
            args = {"_unparsed": raw}
        meta = {}
        extra = _get(tc, "extra_content")
        if extra:
            meta["extra_content"] = extra
        calls.append(ToolCall(id=_get(tc, "id") or uuid.uuid4().hex[:12], name=_get(fn, "name"), args=args, meta=meta))
    usage = _get(resp, "usage")
    details = _get(usage, "prompt_tokens_details") if usage is not None else None
    return ModelStepped(session=session, turn=turn, by=by, text=(_get(msg, "content") or "").strip(),
                        calls=tuple(calls), cut_off=_get(choice, "finish_reason") == "length",
                        usage=Usage(model=_get(resp, "model") or "",
                                    input_tokens=_get(usage, "prompt_tokens", 0) or 0,
                                    output_tokens=_get(usage, "completion_tokens", 0) or 0,
                                    cached_tokens=(_get(details, "cached_tokens", 0) or 0) if details else 0)
                        if usage is not None else None)


def _transient_openai(e: Exception) -> bool:
    import openai

    if isinstance(e, (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError,
                      openai.InternalServerError)):
        return True
    return isinstance(e, openai.APIStatusError) and (e.status_code >= 500 or e.status_code == 429)


class OpenAIModel:
    def __init__(self, model: str, *, base_url: str | None = None, api_key: str | None = None,
                 client: Any = None, by: str = "") -> None:
        self.model, self.by = model, by or model
        if client is None:
            from openai import AsyncOpenAI

            from meeseek.runtime.llm import _MODEL_TIMEOUT_S

            client = AsyncOpenAI(base_url=base_url, api_key=api_key or "unused", timeout=_MODEL_TIMEOUT_S)
        self.client = client

    async def step(self, *, system: str, history: Sequence[Event], tools: Sequence[ToolSpec],
                   session: str, turn: str, schema: Any = None, temperature: float | None = None,
                   max_output_tokens: int | None = None, timeout_s: float | None = None,
                   thinking: str | None = None) -> ModelStepped:
        kwargs: dict[str, Any] = {"model": self.model,
                                  "messages": openai_messages(system, conversation(history),
                                                              google="gemini" in self.model)}
        if tools:
            kwargs["tools"] = openai_tools(tools)
        if schema is not None:
            kwargs["response_format"] = _json_format(schema)
        for key, value in (("temperature", temperature), ("max_tokens", max_output_tokens), ("timeout", timeout_s)):
            if value is not None:
                kwargs[key] = value
        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except Exception as e:
            if _overflow(e):
                raise ContextOverflowError(str(e)[:300]) from e
            if _transient_openai(e):
                raise TransientModelError(f"{type(e).__name__}: {e}") from e
            raise
        return from_openai(resp, session=session, turn=turn, by=self.by)


def _transient_litellm(e: Exception) -> bool:
    import litellm

    kinds = tuple(k for k in (getattr(litellm, n, None) for n in (
        "RateLimitError", "ServiceUnavailableError", "Timeout", "APIConnectionError", "InternalServerError"))
        if isinstance(k, type))
    return isinstance(e, kinds)


# Every call states its output budget: a provider's default can cut a big tool
# call (a file write, a full MR description) off mid-JSON.
MAX_OUTPUT_TOKENS = 16_000


def _without_blanks(messages: list[dict]) -> list[dict]:
    """No message that says nothing — Bedrock refuses a blank one — but every
    tool result, empty or not: each answers a call."""
    return [m for m in messages if m.get("role") == "tool" or m.get("content") or m.get("tool_calls")]


class LitellmModel:
    def __init__(self, model: str, *, by: str = "", **options: Any) -> None:
        self.model, self.by, self.options = model, by or model, options

    async def step(self, *, system: str, history: Sequence[Event], tools: Sequence[ToolSpec],
                   session: str, turn: str, schema: Any = None, temperature: float | None = None,
                   max_output_tokens: int | None = None, timeout_s: float | None = None,
                   thinking: str | None = None) -> ModelStepped:
        import litellm

        from meeseek.runtime.llm import _MODEL_TIMEOUT_S, _mark_cacheable_prefix, _supports_prompt_caching

        messages = _without_blanks(openai_messages(system, conversation(history), google="gemini" in self.model))
        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": max_output_tokens or MAX_OUTPUT_TOKENS,
                                  "timeout": timeout_s or _MODEL_TIMEOUT_S, **self.options, "messages": messages}
        if tools:
            kwargs["tools"] = openai_tools(tools)
        if schema is not None:
            kwargs["response_format"] = _json_format(schema)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if _supports_prompt_caching(self.model):     # the tools and instruction, re-sent every step: cached
            _mark_cacheable_prefix(messages, kwargs.get("tools"))
        try:
            resp = await litellm.acompletion(**kwargs)
        except Exception as e:
            if _overflow(e):
                raise ContextOverflowError(str(e)[:300]) from e
            if _transient_litellm(e):
                raise TransientModelError(f"{type(e).__name__}: {e}") from e
            raise
        return from_openai(resp, session=session, turn=turn, by=self.by)


# --- Gemini through google-genai ---------------------------------------------------

def genai_contents(messages: Sequence[Message], load: Callable[[str], bytes] = _load) -> list:
    from google.genai import types

    out: list = []
    for m in messages:
        if m.role == "assistant":
            parts = [types.Part(text=m.text)] if m.text else []
            for c in m.calls:
                sig = c.meta.get("thought_signature")
                parts.append(types.Part(function_call=types.FunctionCall(id=c.id or None, name=c.name, args=c.args),
                                        thought_signature=base64.b64decode(sig) if sig else SKIP_SIGNATURE.encode()))
            out.append(types.Content(role="model", parts=parts or [types.Part(text="")]))
        elif m.role == "tool":
            part = types.Part(function_response=types.FunctionResponse(id=m.call_id or None, name=m.name,
                                                                         response={"result": m.text}))
            if out and out[-1].role == "user" and all(p.function_response for p in out[-1].parts):
                out[-1].parts.append(part)       # a step's results go back together, as one turn
            else:
                out.append(types.Content(role="user", parts=[part]))
        else:
            parts = [types.Part(inline_data=types.Blob(mime_type=a.mime, data=load(a.ref))) for a in m.attachments]
            if m.text or not parts:
                parts.append(types.Part(text=m.text))
            out.append(types.Content(role="user", parts=parts))
    return out


def genai_tools(tools: Sequence[ToolSpec]) -> list:
    from google.genai import types

    if not tools:
        return []
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=t.name, description=t.description,
                                  parameters_json_schema=t.parameters or {"type": "object", "properties": {}})
        for t in tools])]


def from_genai(resp: Any, *, session: str, turn: str, by: str, model: str) -> ModelStepped:
    cands = getattr(resp, "candidates", None) or []
    parts = list(getattr(getattr(cands[0], "content", None), "parts", None) or []) if cands else []
    text = "".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)).strip()
    calls = []
    for p in parts:
        fc = getattr(p, "function_call", None)
        if fc is None:
            continue
        meta = {}
        if getattr(p, "thought_signature", None):
            meta["thought_signature"] = base64.b64encode(p.thought_signature).decode()
        calls.append(ToolCall(id=fc.id or uuid.uuid4().hex[:12], name=fc.name, args=dict(fc.args or {}), meta=meta))
    um = getattr(resp, "usage_metadata", None)
    usage = Usage(model=getattr(resp, "model_version", None) or model,
                  input_tokens=getattr(um, "prompt_token_count", None) or 0,
                  output_tokens=getattr(um, "candidates_token_count", None) or 0,
                  cached_tokens=getattr(um, "cached_content_token_count", None) or 0) if um else None
    reason = str(getattr(cands[0], "finish_reason", "") or "") if cands else ""
    return ModelStepped(session=session, turn=turn, by=by, text=text, calls=tuple(calls), usage=usage,
                        cut_off="MAX_TOKENS" in reason)


class GenaiModel:
    def __init__(self, model: str, *, client: Any = None, by: str = "") -> None:
        self.model, self.by, self._client = model, by or model, client

    def _get_client(self) -> Any:
        if self._client is None:
            from meeseek.config.llm import get_genai_client

            self._client = get_genai_client()
        return self._client

    async def step(self, *, system: str, history: Sequence[Event], tools: Sequence[ToolSpec],
                   session: str, turn: str, schema: Any = None, temperature: float | None = None,
                   max_output_tokens: int | None = None, timeout_s: float | None = None,
                   thinking: str | None = None) -> ModelStepped:
        from google.genai import types

        from meeseek.runtime.llm import is_transient_failure

        config = types.GenerateContentConfig(
            system_instruction=system or None, tools=genai_tools(tools) or None,
            max_output_tokens=max_output_tokens or MAX_OUTPUT_TOKENS, temperature=temperature,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            response_mime_type="application/json" if schema is not None else None, response_schema=schema,
            thinking_config=types.ThinkingConfig(thinking_level=thinking) if thinking else None,
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)) if timeout_s else None)
        try:
            resp = await self._get_client().aio.models.generate_content(
                model=self.model, contents=genai_contents(conversation(history)), config=config)
        except Exception as e:
            if _overflow(e):
                raise ContextOverflowError(str(e)[:300]) from e
            if is_transient_failure(e):
                raise TransientModelError(f"{type(e).__name__}: {e}") from e
            raise
        return from_genai(resp, session=session, turn=turn, by=self.by, model=self.model)


def build_model(name: str, *, by: str = ""):
    """The model for a tier's id: through the gateway when one is set, else
    Gemini natively, else litellm."""
    from meeseek.config import cfg, secret

    if cfg.models.gateway:
        return OpenAIModel(name, base_url=cfg.models.gateway, api_key=secret("MODEL_GATEWAY_KEY"), by=by)
    if name.startswith("gemini"):
        return GenaiModel(name, by=by)
    from meeseek.runtime.llm import is_claude_model, litellm_id

    model_id = litellm_id(name)
    if (is_claude_model(name) or model_id.startswith("bedrock/")) and not _installed("boto3"):
        # fail at startup, saying how to fix it
        raise RuntimeError(f"model {name!r} runs on Bedrock, which needs boto3: uv sync --extra bedrock")
    return LitellmModel(model_id, by=by)


def _installed(module: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(module) is not None
