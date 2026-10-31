"""Search the web: one Gemini call grounded on Google Search, with its sources.

Gemini's built-in search cannot share a request with function tools, so it
runs as a tool of its own — a separate call on the fast tier — and comes back
as text the agent can read, cite and follow up with fetch_url. Gemini-only,
unlike meeseek's other model calls (runtime/oneshot.py): grounding on Google
Search is Gemini's, so it goes to Gemini directly and says so when
`models.fast` is not a Gemini model.
"""

from __future__ import annotations


def web_search(query: str) -> str:
    """Search the web (Google) and answer from what it finds, with the sources. Use it for what is public
    and current — a library's docs, an error message, news, a vendor's status — not for the team's own
    systems or the knowledge base. Follow a source up with fetch_url when the details matter.

    Args:
        query: what to look up: a search query or a plain question
    """
    from google.genai import types

    from meeseek.config import cfg
    from meeseek.config.llm import get_genai_client

    model = cfg.models.fast
    if not model.startswith("gemini"):
        return f"Error: web search runs on a Gemini model; models.fast is {model!r}."
    client = get_genai_client()  # hold the reference: a temporary Client is closed on GC mid-request
    resp = client.models.generate_content(
        model=model, contents=query,
        config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]))
    text = (resp.text or "").strip()
    sources = []
    for cand in resp.candidates or []:
        meta = getattr(cand, "grounding_metadata", None)
        for chunk in (getattr(meta, "grounding_chunks", None) or []):
            web = getattr(chunk, "web", None)
            if web is not None and web.uri and web.uri not in {u for _, u in sources}:
                sources.append((web.title or web.uri, web.uri))
    if not text:
        return f"Nothing found for {query!r}."
    if sources:
        text += "\n\nSources:\n" + "\n".join(f"- {title}: {uri}" for title, uri in sources[:8])
    return text
