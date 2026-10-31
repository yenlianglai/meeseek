"""Usage accounting: session scoping and reading back what ADK stores."""

import json
import sqlite3
import time

from meeseek.runtime import usage


def _db(tmp_path, events):
    """A minimal stand-in for ADK's sessions.db, in its real schema."""
    path = tmp_path / "sessions.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE events (id TEXT, app_name TEXT, user_id TEXT, session_id TEXT,"
        " invocation_id TEXT, timestamp REAL, event_data TEXT)"
    )
    now = time.time()
    for i, e in enumerate(events):
        con.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
            (str(i), "agents", "u", "s", "inv", now, json.dumps(e)),
        )
    con.commit()
    con.close()
    return path


def _event(model, author, prompt, candidates, thoughts=0):
    return {
        "model_version": model,
        "author": author,
        "usage_metadata": {
            "prompt_token_count": prompt,
            "candidates_token_count": candidates,
            "thoughts_token_count": thoughts,
            "total_token_count": prompt + candidates + thoughts,
        },
    }


def test_summary_buckets_by_model_source_and_day(tmp_path):
    db = _db(tmp_path, [
        _event("gemini-3.6-flash", "code_agent", 100, 10),
        _event("gemini-3.6-flash", "code_agent", 200, 20),
        _event("us.anthropic.claude-opus-5", "graph-node", 50, 5),
    ])
    s = usage.summary(days=1, db=db)

    assert s["totals"]["requests"] == 3
    assert s["totals"]["prompt"] == 350
    assert {r["model"]: r["requests"] for r in s["by_model"]} == {
        "gemini-3.6-flash": 2, "us.anthropic.claude-opus-5": 1}
    # workflow loops bucket by the author we stamp on the event
    assert {r["source"]: r["requests"] for r in s["by_source"]} == {
        "code_agent": 2, "graph-node": 1}
    assert len(s["by_day"]) == 1


def test_thinking_tokens_count_as_output(tmp_path):
    """Providers bill thinking inside output — the dashboard must match."""
    db = _db(tmp_path, [
        _event("claude", "graph-node", 10, 5, thoughts=40),
    ])
    assert usage.summary(days=1, db=db)["totals"]["output"] == 45


def test_events_without_usage_are_ignored(tmp_path):
    """Tool-result and transfer events carry no usage and aren't requests."""
    db = _db(tmp_path, [
        {"author": "code_agent", "content": {"role": "user"}},
        _event("gemini-3.6-flash", "code_agent", 1, 1),
    ])
    assert usage.summary(days=1, db=db)["totals"]["requests"] == 1


def test_missing_db_yields_an_empty_summary(tmp_path):
    db = tmp_path / "nope.db"
    s = usage.summary(days=30, db=db)
    assert s["totals"]["requests"] == 0
    assert s["by_model"] == [] and s["by_day"] == []


PRICE = {"input_cost_per_token": 1e-6, "output_cost_per_token": 4e-6, "cache_read_input_token_cost": 1e-7,
         "input_cost_per_token_above_200k_tokens": 2e-6, "output_cost_per_token_above_200k_tokens": 8e-6}


def test_cost_bills_cached_input_at_the_cache_rate(monkeypatch):
    monkeypatch.setattr(usage, "price_of", lambda m: PRICE if m == "m" else None)
    # 1000 prompt tokens, 400 of them cached; thinking is already inside output
    assert usage.cost_usd("m", prompt=1000, cached=400, output=100) == (600 * 1e-6 + 400 * 1e-7 + 100 * 4e-6)


def test_a_long_prompt_is_billed_at_its_tier(monkeypatch):
    monkeypatch.setattr(usage, "price_of", lambda m: PRICE)
    assert usage.cost_usd("m", prompt=300_000, cached=0, output=10) == 300_000 * 2e-6 + 10 * 8e-6


def test_the_summary_carries_cost_the_rate_and_what_it_could_not_price(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "price_of", lambda m: PRICE if m == "priced" else None)
    db = _db(tmp_path, [_event("priced", "code_agent", 1000, 100), _event("mystery", "code_agent", 50, 5)])
    s = usage.summary(days=1, db=db)
    assert round(s["totals"]["cost_usd"], 6) == round(1000 * 1e-6 + 100 * 4e-6, 6)
    assert s["unpriced"] == ["mystery"]
    assert s["usd_to_twd"] > 0
    assert {r["model"]: round(r["cost_usd"], 6) for r in s["by_model"]}["mystery"] == 0


def test_the_models_in_use_are_in_the_price_table():
    for m in ("gemini-3.8-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash"):
        assert usage.price_of(m), m


class TestPriceTable:
    """litellm falls back to its shipped table when the fetch fails; the last
    full table fetched fills the models that one lacks."""

    def _table(self, monkeypatch, tmp_path, live):
        import litellm

        monkeypatch.setattr(litellm, "model_cost", live)
        usage.price_table.cache_clear()
        try:
            return usage.price_table()
        finally:
            usage.price_table.cache_clear()
            usage.price_of.cache_clear()

    def test_a_fresh_fetch_is_kept_and_fills_a_failed_one(self, monkeypatch, tmp_path):
        fetched = {"old": {"input_cost_per_token": 1}, "new": {"input_cost_per_token": 2}}
        assert self._table(monkeypatch, tmp_path, fetched) == fetched
        shipped = {"old": {"input_cost_per_token": 1}}
        assert self._table(monkeypatch, tmp_path, shipped)["new"] == {"input_cost_per_token": 2}

    def test_no_saved_table_is_litellms_alone(self, monkeypatch, tmp_path):
        shipped = {"old": {"input_cost_per_token": 1}}
        assert self._table(monkeypatch, tmp_path, shipped) == shipped


def test_direct_calls_count_in_the_summary_by_source(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "price_of", lambda m: PRICE if m == "m" else None)
    usage.direct_log().write_text(json.dumps({"ts": time.time(), "source": "meeting_transcript", "model": "m",
                                            "prompt": 1000, "cached": 0, "output": 100, "thoughts": 40}) + "\n")
    s = usage.summary(days=1, db=_db(tmp_path, []))
    assert s["totals"]["requests"] == 1 and s["totals"]["output"] == 100
    assert s["by_source"][0]["source"] == "meeting_transcript"
    assert s["totals"]["cost_usd"] == usage.cost_usd("m", 1000, 0, 100) > 0
