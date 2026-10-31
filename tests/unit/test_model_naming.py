"""Claude naming resolves the same on both model paths."""

import pytest

from meeseek.runtime.llm import is_claude_model, litellm_id


@pytest.mark.parametrize("name", ["claude-opus-5", "claude-opus-4-8", "claude-sonnet-4-5"])
def test_alias_expands_to_a_bedrock_route(name):
    assert litellm_id(name).startswith("bedrock/us.anthropic.claude")


def test_already_qualified_ids_pass_through():
    full = "bedrock/us.anthropic.claude-opus-5"
    assert litellm_id(full) == full
    assert litellm_id("us.anthropic.claude-opus-5") == full


def test_detection_covers_both_conventions():
    assert is_claude_model("claude-opus-5")
    assert is_claude_model("bedrock/us.anthropic.claude-opus-5")
    assert not is_claude_model("gemini-3.6-flash")


def test_claude_alias_does_not_fall_through_to_gemini(monkeypatch):
    """A bare Claude alias resolves to Bedrock, never to a Gemini client."""
    import importlib.util

    from meeseek.adapters.llm import LitellmModel, build_model

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    model = build_model("claude-opus-4-8")
    assert isinstance(model, LitellmModel) and model.model == "bedrock/us.anthropic.claude-opus-4-8"


def test_a_bedrock_model_without_boto3_fails_at_startup_and_says_how_to_fix(monkeypatch):
    import importlib.util

    import pytest

    from meeseek.adapters.llm import build_model

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(RuntimeError, match="uv sync --extra bedrock"):
        build_model("bedrock/us.anthropic.claude-opus-4-8")


def test_cacheable_gate_counts_tools_not_just_the_prompt():
    """A sub-task's system prompt is ~900 chars — the cacheable bulk is its tools."""
    from meeseek.runtime.llm import _mark_cacheable_prefix

    def msgs():
        return [{"role": "system", "content": "x" * 900}]

    assert _mark_cacheable_prefix(msgs(), [{"name": "t", "d": "y" * 5000}]) is True
    assert _mark_cacheable_prefix(msgs(), None) is False


def test_cacheable_prefix_marks_one_ephemeral_breakpoint():
    from meeseek.runtime.llm import _mark_cacheable_prefix

    messages = [{"role": "system", "content": "x" * 5000}]
    _mark_cacheable_prefix(messages)
    blocks = messages[0]["content"]
    assert [b["cache_control"] for b in blocks] == [{"type": "ephemeral"}]
    assert blocks[0]["text"] == "x" * 5000


def test_mark_cacheable_prefix_is_idempotent_across_retries():
    """Retries reuse the same messages list; a second breakpoint would be invalid."""
    from meeseek.runtime.llm import _mark_cacheable_prefix

    messages = [{"role": "system", "content": "x" * 5000}, {"role": "user", "content": "hi"}]
    assert _mark_cacheable_prefix(messages) is True
    assert _mark_cacheable_prefix(messages) is False
    assert len(messages[0]["content"]) == 1


def test_mark_cacheable_prefix_skips_short_prompts():
    from meeseek.runtime.llm import _mark_cacheable_prefix

    messages = [{"role": "system", "content": "short"}, {"role": "user", "content": "hi"}]
    assert _mark_cacheable_prefix(messages) is False
    assert messages[0]["content"] == "short"


def test_every_litellm_call_states_its_output_budget_and_caches_claudes_prefix(monkeypatch):
    """A provider's default can cut a big tool call off mid-JSON; Claude's tools
    and instruction, re-sent every step, are marked for its prompt cache."""
    import asyncio

    import litellm

    from meeseek.adapters.llm import MAX_OUTPUT_TOKENS, LitellmModel

    sent = {}

    async def acompletion(**kw):
        sent.update(kw)
        return {"model": "m", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    monkeypatch.setattr("meeseek.runtime.llm._supports_prompt_caching", lambda model: True)
    asyncio.run(LitellmModel("bedrock/us.anthropic.claude-opus-4-8").step(
        system="x" * 6000, history=[], tools=[], session="s", turn="t"))
    assert sent["max_tokens"] == MAX_OUTPUT_TOKENS
    assert sent["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
