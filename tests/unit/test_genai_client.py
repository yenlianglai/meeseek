"""Every direct Gemini call is bounded: the SDK's default is to wait forever."""

from google import genai

from meeseek.config import llm


def _captured(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(genai, "Client", FakeClient)
    return seen


def test_a_client_carries_a_timeout_by_default(monkeypatch):
    seen = _captured(monkeypatch)
    llm.get_genai_client()
    assert seen["http_options"].timeout == 300_000   # milliseconds


def test_a_caller_can_shorten_or_lengthen_it(monkeypatch):
    seen = _captured(monkeypatch)
    llm.get_genai_client(timeout_s=60)
    assert seen["http_options"].timeout == 60_000


def test_every_engine_model_call_is_bounded(monkeypatch):
    """Gemini's client is bounded where it is made (above); a litellm call and an
    OpenAI-compatible client carry the same bound."""
    import asyncio

    import litellm

    from meeseek.adapters.llm import LitellmModel, OpenAIModel
    from meeseek.runtime.llm import _MODEL_TIMEOUT_S

    sent = {}

    async def acompletion(**kw):
        sent.update(kw)
        return {"model": "m", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    asyncio.run(LitellmModel("openai/gpt-x").step(system="", history=[], tools=[], session="s", turn="t"))
    assert sent["timeout"] == _MODEL_TIMEOUT_S
    assert OpenAIModel("gpt-x", base_url="http://localhost:1/v1").client.timeout == _MODEL_TIMEOUT_S


def test_a_timeout_reads_as_transient_so_the_turn_resumes():
    import httpx

    from meeseek.runtime.llm import is_transient_failure

    try:
        try:
            raise TimeoutError("timed out")
        except TimeoutError as inner:
            raise httpx.ReadTimeout("read timed out") from inner
    except httpx.ReadTimeout as e:
        assert is_transient_failure(e)


def test_every_direct_call_is_recorded_under_its_caller(monkeypatch):
    """Direct calls are not ADK turns: without this ledger nothing counts them."""
    import json
    from types import SimpleNamespace

    from meeseek.runtime import usage

    meta = SimpleNamespace(prompt_token_count=1000, cached_content_token_count=200,
                           candidates_token_count=50, thoughts_token_count=30)

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = SimpleNamespace(generate_content=lambda **kw: SimpleNamespace(
                usage_metadata=meta, model_version="gemini-3.8-flash", text="ok"))

    monkeypatch.setattr(genai, "Client", FakeClient)
    resp = llm.get_genai_client().models.generate_content(model="gemini-3.8-flash", contents="hi")
    assert resp.text == "ok"
    line = json.loads(usage.direct_log().read_text())
    assert line["source"] == "test_genai_client"   # the calling module, unless named
    assert (line["prompt"], line["cached"], line["output"], line["thoughts"]) == (1000, 200, 80, 30)

    llm.get_genai_client(source="meeting_transcript").models.generate_content(model="m", contents="x")
    assert json.loads(usage.direct_log().read_text().splitlines()[1])["source"] == "meeting_transcript"


def test_a_broken_ledger_never_fails_the_call(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from meeseek.runtime import usage

    monkeypatch.setattr(usage, "direct_log", lambda: tmp_path / "is-a-dir")
    (tmp_path / "is-a-dir").mkdir()

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = SimpleNamespace(generate_content=lambda **kw: SimpleNamespace(usage_metadata=None, text="ok"))

    monkeypatch.setattr(genai, "Client", FakeClient)
    assert llm.get_genai_client().models.generate_content(model="m", contents="x").text == "ok"
