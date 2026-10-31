"""Web search: a grounded Gemini call, its sources listed."""

from types import SimpleNamespace as NS

from meeseek.tools import web_search as ws


def _client(text, chunks):
    resp = NS(text=text, candidates=[NS(grounding_metadata=NS(grounding_chunks=[
        NS(web=NS(title=t, uri=u)) for t, u in chunks]))])
    return NS(models=NS(generate_content=lambda **kw: resp))


def test_the_answer_comes_with_its_sources(monkeypatch):
    monkeypatch.setattr("meeseek.config.llm.get_genai_client",
                        lambda: _client("FastAPI 0.115 dropped Python 3.8.", [("Release notes", "https://x/notes"),
                                                                             ("Release notes", "https://x/notes")]))
    out = ws.web_search("fastapi 0.115 python 3.8")
    assert out.startswith("FastAPI 0.115 dropped Python 3.8.") and out.count("https://x/notes") == 1


def test_a_non_gemini_fast_tier_is_said_plainly(monkeypatch):
    from meeseek.config import config

    monkeypatch.setattr(config().models, "fast", "openai/gpt-x")
    assert ws.web_search("x").startswith("Error: web search runs on a Gemini model")


def test_every_engine_agent_can_search():
    from meeseek import engine

    assert all("web_search" in {t.name for t in a.tools} for a in engine.agents())
