"""Gemini client factory — env-driven, single ADC auth path.

GOOGLE_GENAI_USE_VERTEXAI / GOOGLE_CLOUD_PROJECT are injected by the
config loader from project.yaml's `google_cloud:` block.
"""

import sys

from google import genai
from google.genai import types


def get_genai_client(timeout_s: float = 300, source: str = "") -> genai.Client:
    """Return a Gemini client using the environment-configured auth path.

    Every request is bounded by `timeout_s`. The SDK's own default is no
    timeout at all, and a connection that dies without a reset (a VPN
    reconnecting, Wi-Fi changing) then holds its thread forever — once the
    whole triggers job, every source with it, until a restart.

    Every generate_content is recorded (runtime/usage.py) under `source`,
    by default the calling module's name: these calls are not agent turns,
    so nothing else would count them.
    """
    source = source or sys._getframe(1).f_globals.get("__name__", "").rsplit(".", 1)[-1] or "direct"
    client = genai.Client(http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))
    return _Recorded(client, source)  # type: ignore[return-value]


class _Recorded:
    """The client, with `models.generate_content` recording its usage. It
    holds the real client: a client nobody holds is closed under its call."""

    def __init__(self, client: genai.Client, source: str):
        self._client, self._source = client, source

    @property
    def models(self):
        return _RecordedModels(self._client.models, self._source)

    def __getattr__(self, name):
        return getattr(self._client, name)


class _RecordedModels:
    def __init__(self, models, source: str):
        self._models, self._source = models, source

    def generate_content(self, *, model: str, **kwargs):
        resp = self._models.generate_content(model=model, **kwargs)
        from meeseek.runtime.usage import record_direct

        record_direct(self._source, getattr(resp, "model_version", None) or model,
                      getattr(resp, "usage_metadata", None))
        return resp

    def __getattr__(self, name):
        return getattr(self._models, name)
