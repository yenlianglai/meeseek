"""The runner's model adapters: what each provider is sent and how its answer reads back."""

import asyncio
import base64
from types import SimpleNamespace as NS

import httpx
import openai
import pytest
from google.genai import types

from meeseek.adapters import llm
from meeseek.adapters.memory import MemoryEventStore
from meeseek.core import events as ev
from meeseek.core.tools import tool
from meeseek.core.turn import Message, TransientModelError, TurnRunner


@tool()
def lookup(key: str) -> str:
    """Look a ticket up.

    Args:
        key: the ticket
    """
    return f"{key} is In Review"


def _history():
    call = ev.ToolCall(id="c1", name="lookup", args={"key": "ACME-1"}, meta={"thought_signature": base64.b64encode(b"sig").decode()})
    return [Message(role="user", text="ACME-1?"),
            Message(role="assistant", calls=(call,)),
            Message(role="tool", text="ACME-1 is In Review", call_id="c1", name="lookup")]


class TestOpenAIFormat:
    def test_the_conversation_and_tools_as_openai_reads_them(self):
        msgs = llm.openai_messages("be brief", _history())
        assert msgs[0] == {"role": "system", "content": "be brief"}
        assert msgs[1] == {"role": "user", "content": "ACME-1?"}
        assert msgs[2]["content"] is None and msgs[2]["tool_calls"][0]["function"] == {
            "name": "lookup", "arguments": '{"key": "ACME-1"}'}
        assert msgs[3] == {"role": "tool", "tool_call_id": "c1", "name": "lookup", "content": "ACME-1 is In Review"}
        [t] = llm.openai_tools([lookup.spec])
        assert t["function"]["parameters"]["properties"]["key"]["description"] == "the ticket"

    def test_gemini_behind_the_gateway_gets_its_signature_back(self):
        msgs = llm.openai_messages("", _history(), google=True)
        assert msgs[1]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": base64.b64encode(b"sig").decode()}}
        fresh = [Message(role="assistant", calls=(ev.ToolCall(id="x", name="lookup"),))]
        assert llm.openai_messages("", fresh, google=True)[0]["tool_calls"][0]["extra_content"] == {
            "google": {"thought_signature": llm.SKIP_SIGNATURE}}

    def test_an_answer_reads_back_with_its_calls_and_usage(self):
        resp = {"model": "m", "choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c9", "type": "function", "function": {"name": "lookup", "arguments": '{"key":"ACME-2"}'},
             "extra_content": {"google": {"thought_signature": "abc"}}}]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 4}}}
        step = llm.from_openai(resp, session="s", turn="t", by="router")
        assert step.calls[0] == ev.ToolCall(id="c9", name="lookup", args={"key": "ACME-2"},
                                            meta={"extra_content": {"google": {"thought_signature": "abc"}}})
        assert step.usage == ev.Usage(model="m", input_tokens=10, output_tokens=3, cached_tokens=4)


class _FakeOpenAI:
    def __init__(self, *answers):
        self.answers, self.sent = list(answers), []
        self.chat = NS(completions=NS(create=self._create))

    async def _create(self, **kwargs):
        self.sent.append(kwargs)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def _say(text):
    return {"model": "m", "choices": [{"message": {"content": text}}]}


def _call(cid, name, args):
    import json
    return {"model": "m", "choices": [{"message": {"content": None, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def test_a_turn_through_an_openai_compatible_endpoint():
    fake = _FakeOpenAI(_call("c1", "lookup", {"key": "ACME-1"}), _say("In Review."))
    runner = TurnRunner(model=llm.OpenAIModel("gpt-x", client=fake), tools=[lookup.spec], store=MemoryEventStore())
    res = asyncio.run(runner.run("s", "ACME-1?"))
    assert res.reply == "In Review."
    assert fake.sent[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "name": "lookup",
                                             "content": "ACME-1 is In Review"}


@pytest.mark.parametrize("error,transient", [
    (openai.APIConnectionError(request=httpx.Request("POST", "http://gw")), True),
    (openai.RateLimitError("slow down", response=httpx.Response(429, request=httpx.Request("POST", "http://gw")), body=None), True),
    (openai.BadRequestError("bad", response=httpx.Response(400, request=httpx.Request("POST", "http://gw")), body=None), False),
])
def test_only_a_failure_worth_retrying_is_transient(error, transient):
    model = llm.OpenAIModel("gpt-x", client=_FakeOpenAI(error))
    expected = TransientModelError if transient else type(error)
    with pytest.raises(expected):
        asyncio.run(model.step(system="", history=[], tools=[], session="s", turn="t"))


class TestGenai:
    def test_the_conversation_as_gemini_reads_it(self):
        contents = llm.genai_contents(_history() + [
            Message(role="tool", text="second", call_id="c2", name="lookup")])
        assert [c.role for c in contents] == ["user", "model", "user"]
        call = contents[1].parts[0]
        assert call.function_call.name == "lookup" and call.thought_signature == b"sig"
        assert [p.function_response.response for p in contents[2].parts] == [
            {"result": "ACME-1 is In Review"}, {"result": "second"}]           # one step's results together
        [decls] = llm.genai_tools([lookup.spec])
        assert decls.function_declarations[0].name == "lookup"

    def test_an_answer_reads_back_with_its_signature(self):
        resp = types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(role="model", parts=[
                types.Part(text="thinking", thought=True),
                types.Part(function_call=types.FunctionCall(id="f1", name="lookup", args={"key": "ACME-3"}),
                           thought_signature=b"s2")]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=7, candidates_token_count=2))
        step = llm.from_genai(resp, session="s", turn="t", by="router", model="gemini-x")
        assert step.text == "" and step.calls[0].args == {"key": "ACME-3"}
        assert base64.b64decode(step.calls[0].meta["thought_signature"]) == b"s2"
        assert step.usage.input_tokens == 7

    def test_a_turn_through_gemini(self):
        answers = [
            types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[
                types.Part(function_call=types.FunctionCall(id="f1", name="lookup", args={"key": "ACME-1"}),
                           thought_signature=b"s")]))]),
            types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[
                types.Part(text="In Review.")]))]),
        ]
        sent = []

        async def generate_content(**kw):
            sent.append(kw)
            return answers.pop(0)

        client = NS(aio=NS(models=NS(generate_content=generate_content)))
        runner = TurnRunner(model=llm.GenaiModel("gemini-x", client=client), tools=[lookup.spec],
                            store=MemoryEventStore(), system=lambda t: "be brief")
        assert asyncio.run(runner.run("s", "ACME-1?")).reply == "In Review."
        second = sent[1]
        assert second["config"].system_instruction == "be brief"
        assert second["contents"][1].parts[0].thought_signature == b"s"   # the signature goes back


def test_the_gateway_takes_every_model_when_set(monkeypatch):
    from meeseek.config import cfg

    monkeypatch.setattr(cfg.models, "gateway", "http://localhost:8080/v1")
    assert isinstance(llm.build_model("gemini-2.5-flash"), llm.OpenAIModel)
    monkeypatch.setattr(cfg.models, "gateway", "")
    assert isinstance(llm.build_model("gemini-2.5-flash"), llm.GenaiModel)
    monkeypatch.setattr(llm, "_installed", lambda m: True)
    assert isinstance(llm.build_model("bedrock/us.anthropic.claude-opus-4-8"), llm.LitellmModel)


def test_a_bedrock_model_without_boto3_says_how_to_fix_it(monkeypatch):
    import pytest

    monkeypatch.setattr(llm, "_installed", lambda m: False)
    with pytest.raises(RuntimeError, match="--extra bedrock"):
        llm.build_model("bedrock/us.anthropic.claude-opus-4-8")


def test_files_lead_the_text_for_each_provider():
    msg = Message(role="user", text="這張圖？", attachments=(ev.Attachment(mime="image/png", ref="r1"),
                                                         ev.Attachment(mime="application/pdf", ref="r2")))
    load = {"r1": b"PNG", "r2": b"%PDF"}.__getitem__
    [user] = llm.openai_messages("", [msg], load=load)
    assert user["content"][0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,UE5H"}}
    assert user["content"][1]["file"]["file_data"] == "data:application/pdf;base64,JVBERg=="
    assert user["content"][2] == {"type": "text", "text": "這張圖？"}
    [content] = llm.genai_contents([msg], load=load)
    assert [p.inline_data.data for p in content.parts[:2]] == [b"PNG", b"%PDF"] and content.parts[2].text == "這張圖？"


def test_only_the_latest_files_are_shown_again():
    from meeseek.core.turn import conversation

    def said(i):
        return ev.UserSaid(session="s", turn=f"t{i}", text=f"pic {i}", attachments=(ev.Attachment(mime="image/png", ref=f"r{i}"),))

    msgs = conversation([said(1), said(2), said(3)])
    assert [len(m.attachments) for m in msgs] == [0, 1, 1]
    assert "no longer shown" in msgs[0].text and msgs[0].text.startswith("pic 1")


def _bad_request(message):
    return openai.BadRequestError(message, response=httpx.Response(400, request=httpx.Request("POST", "http://gw")),
                                  body=None)


@pytest.mark.parametrize("message", [
    "This model's maximum context length is 128000 tokens. However, your messages resulted in 140000 tokens.",
    "The input token count (1100000) exceeds the maximum number of tokens allowed (1048576).",
    "prompt is too long: 210000 tokens > 200000 maximum",
])
def test_a_request_over_the_context_window_is_an_overflow_not_a_failure(message):
    from meeseek.core.turn import ContextOverflowError

    model = llm.OpenAIModel("gpt-x", client=_FakeOpenAI(_bad_request(message)))
    with pytest.raises(ContextOverflowError):
        asyncio.run(model.step(system="", history=[], tools=[], session="s", turn="t"))


def test_a_response_that_hit_its_output_limit_says_so():
    import json

    cut = {"model": "m", "choices": [{"finish_reason": "length", "message": {"content": None, "tool_calls": [
        {"id": "w", "type": "function", "function": {"name": "post", "arguments": '{"text": "very lo'}}]}}]}
    step = llm.from_openai(cut, session="s", turn="t", by="b")
    assert step.cut_off and step.calls[0].args == {"_unparsed": '{"text": "very lo'}
    whole = llm.from_openai(json.loads(json.dumps(_say("ok"))), session="s", turn="t", by="b")
    assert not whole.cut_off
    gemini = NS(candidates=[NS(content=NS(parts=[NS(text="partial", thought=False, function_call=None)]),
                               finish_reason=types.FinishReason.MAX_TOKENS)], usage_metadata=None)
    assert llm.from_genai(gemini, session="s", turn="t", by="b", model="gemini-x").cut_off
