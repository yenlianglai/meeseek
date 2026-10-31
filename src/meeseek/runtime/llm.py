"""The model every agent runs on: Gemini or LiteLLM (Bedrock Claude etc.),
wrapped so history is trimmed before each call and overflow, transient errors
and truncated tool calls are retried instead of killing the turn.

Gemini credentials are env-driven (google-genai reads them directly):
GOOGLE_GENAI_USE_VERTEXAI=TRUE with ADC and GOOGLE_CLOUD_PROJECT/LOCATION, or
GEMINI_API_KEY when Vertex is off.
"""

from __future__ import annotations

import logging



logger = logging.getLogger(__name__)


# A model call that hears nothing for this long is dead, not slow. After a
# Wi-Fi or VPN switch a connection can die without a reset, and the SDK's
# default is to wait forever; bounded, it fails as a timeout, which is
# transient, so the turn resumes from its last step (models.transient_retries).
_MODEL_TIMEOUT_S = 300


def _is_transient(err: Exception) -> bool:
    """429 (quota blip) or any 5xx server error — worth a backoff retry."""
    from google.genai.errors import APIError, ServerError

    if isinstance(err, ServerError):
        return True
    code = getattr(err, "code", None) if isinstance(err, APIError) else None
    return code == 429


_TRANSIENT_NAMES = ("TimeoutException", "ConnectError", "RemoteProtocolError", "ReadError",
                    "WriteError", "PoolTimeout", "TransportError", "ServerDisconnectedError",
                    "ResourceExhausted")


def is_transient_failure(err: BaseException) -> bool:
    """A failure worth resuming the turn for: quota, server error, timeout or a
    dropped connection, anywhere in the exception's cause chain (plugins and
    callbacks wrap what they raise)."""
    seen = 0
    while err is not None and seen < 5:
        if _is_transient(err) or isinstance(err, (TimeoutError, ConnectionError)):
            return True
        if any(n in type(err).__name__ for n in _TRANSIENT_NAMES):
            return True
        err = err.__cause__ or err.__context__
        seen += 1
    return False


def access_failure(err: BaseException | None) -> str:
    """"login" when the Google login is missing or no longer valid, "denied"
    when the signed-in account lacks a permission on the Google project, ""
    otherwise — anywhere in the exception's cause chain. Neither is transient:
    a retry gets the same answer until someone changes the login or IAM."""
    from google.auth.exceptions import DefaultCredentialsError, RefreshError
    from google.genai.errors import APIError

    seen = 0
    while err is not None and seen < 5:
        if isinstance(err, (RefreshError, DefaultCredentialsError)):
            return "login"
        if isinstance(err, APIError):
            # A disabled API is also a 403, but no role fixes it.
            if err.code == 401:
                return "login"
            if err.code == 403 and "SERVICE_DISABLED" not in str(err):
                return "denied"
        err = err.__cause__ or err.__context__
        seen += 1
    return ""


# project.yaml.example's value: a project nobody has, so a 403 on it means "fill it in".
PLACEHOLDER_PROJECT = "your-gcp-project"


def failure_message(err: BaseException) -> str:
    """What a person reads when a model call or a whole turn fails."""
    from meeseek.config import cfg
    from meeseek.context import prompts

    detail = f"{type(err).__name__}: {str(err)[:300]}"
    kind = access_failure(err)
    if kind == "denied" and PLACEHOLDER_PROJECT in (cfg.google_cloud.project, cfg.google_cloud.quota_project):
        return prompts.text("runtime/llm#unfilled", project=PLACEHOLDER_PROJECT, detail=detail)
    if kind == "denied":
        projects = " / ".join(dict.fromkeys(
            p for p in (cfg.google_cloud.project, cfg.google_cloud.quota_project) if p)) or "?"
        return prompts.text("runtime/llm#no_access", projects=projects, detail=detail)
    if kind == "login":
        return prompts.text("runtime/llm#login_invalid", detail=detail)
    return detail


# Anthropic 的最小可快取長度是 1024 token（Haiku 2048）；粗估 4 chars/token，
# 抓 ~1100 token 的門檻。低於最小值時 cache_control 會被忽略（不報錯也不多收費）。
_MIN_CACHEABLE_CHARS = 4500


# Claude on Bedrock: friendly alias → the inference-profile id litellm routes to.
# US cross-region profiles. Older models carry a date+version suffix; 4.7+ don't.
_CLAUDE_ALIASES = {
    "claude-opus-5": "us.anthropic.claude-opus-5",
    "claude-opus-4-8": "us.anthropic.claude-opus-4-8",
    "claude-opus-4-5": "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "claude-sonnet-4-5": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
}


def is_claude_model(name: str) -> bool:
    """True for any spelling of a Claude model, in either convention."""
    return name.startswith("claude") or "anthropic.claude" in name


def litellm_id(name: str) -> str:
    """Friendly alias → litellm id. Config can name a model either way."""
    if name.startswith("bedrock/"):
        return name
    return f"bedrock/{_CLAUDE_ALIASES.get(name, name)}"


def _supports_prompt_caching(model: str) -> bool:
    """litellm 的 model registry 說了算；查不到就當作不支援。"""
    try:
        from litellm.utils import supports_prompt_caching

        return bool(supports_prompt_caching(model))
    except Exception:
        return False


def _mark_cacheable_prefix(messages: list, tools: list | None = None) -> bool:
    """在 system 訊息尾端插入 cache_control 斷點。

    Anthropic 的快取前綴順序是 tools → system → messages，所以設在 system 的
    斷點會連 tools schema 一起快取——agent loop 每輪重送且不變的部分。門檻算
    tools + system 兩者。冪等：重試沿用同一個 messages list，已是 list 形式就不再加。
    """
    import json

    for m in messages or []:
        if m.get("role") != "system":
            continue
        content = m.get("content")
        if isinstance(content, list):
            return False  # 已標記過
        if not isinstance(content, str):
            return False
        try:
            tools_chars = len(json.dumps(tools or []))
        except (TypeError, ValueError):
            tools_chars = 0
        if len(content) + tools_chars < _MIN_CACHEABLE_CHARS:
            return False
        m["content"] = [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
        return True
    return False


