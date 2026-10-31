"""LLM usage accounting — every model call this server makes, in one place.

Every turn persists session events carrying `model_version` and
`usage_metadata`, so reporting is a query over the session history
(runtime/history.py); nothing is recorded here. Claude Code turns are read from their own log.

Workflow turns run under the `workflow` user (engine.headless_turn), so
they stay out of the chat sidebar (the console lists `webui`) and out of any
conversation the router would replay as history.
"""

from __future__ import annotations

import functools
import json
import time
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

from meeseek.config.paths import home
from meeseek.runtime.sessions import (  # noqa: F401 — APP_NAME/WORKFLOW_USER re-exported
    APP_NAME,
    WORKFLOW_USER,
    read_only_connection,
)


# --------------------------------------------------------------------------
# reader
# --------------------------------------------------------------------------


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")


def _blank() -> dict:
    return {"requests": 0, "prompt": 0, "output": 0, "cached": 0, "total": 0, "cost_usd": 0.0}


def _add(bucket: dict, prompt: int, output: int, cached: int, total: int, cost: float) -> None:
    bucket["requests"] += 1
    bucket["prompt"] += prompt
    bucket["output"] += output
    bucket["cached"] += cached
    bucket["total"] += total
    bucket["cost_usd"] += cost


# Model calls made outside an agent turn (runtime/oneshot.py: transcripts, notes, triage, review_page…):


# config/llm.py's client writes one line per call here, by source.
def direct_log() -> Path:
    return home().data / "genai_usage.jsonl"


def record_direct(source: str, model: str, usage_metadata) -> None:
    """One direct call's tokens. Never raises: accounting must not fail a call."""
    import json

    try:
        u = usage_metadata
        thoughts = int(getattr(u, "thoughts_token_count", 0) or 0) if u else 0
        line = {
            "ts": time.time(), "source": source, "model": model,
            "prompt": int(getattr(u, "prompt_token_count", 0) or 0) if u else 0,
            "cached": int(getattr(u, "cached_content_token_count", 0) or 0) if u else 0,
            "output": (int(getattr(u, "candidates_token_count", 0) or 0) if u else 0) + thoughts,
            "thoughts": thoughts,   # inside output; kept to see what thinking costs
        }
        direct_log().parent.mkdir(parents=True, exist_ok=True)
        with direct_log().open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:  # noqa: BLE001
        pass


def record_usage(source: str, usage) -> None:
    """One direct call's tokens, from any model (a core Usage). Never raises."""
    import json

    try:
        line = {"ts": time.time(), "source": source, "model": usage.model, "prompt": int(usage.input_tokens or 0),
                "cached": int(usage.cached_tokens or 0), "output": int(usage.output_tokens or 0), "thoughts": 0}
        direct_log().parent.mkdir(parents=True, exist_ok=True)
        with direct_log().open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:  # noqa: BLE001
        pass


_TIER = 200_000  # prompts above this are billed at the "above_200k" rates where a model has them


# the last full price table litellm fetched
def prices_file() -> Path:
    return home().data / "litellm_prices.json"


@functools.lru_cache(maxsize=1)
def price_table() -> dict:
    """litellm's price table, kept on disk. litellm fetches the current table at
    import and, when the fetch fails, silently falls back to the copy it ships
    with — older, without the newest models. The last full table fetched fills
    that gap, so a flaky network does not make a model free."""
    try:
        import litellm
    except ImportError:  # pragma: no cover — litellm is a dependency
        return {}
    live = litellm.model_cost
    try:
        saved = json.loads(prices_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if len(live) > len(saved):   # a fresh fetch: keep it for the next time one fails
        try:
            prices_file().parent.mkdir(parents=True, exist_ok=True)
            tmp = prices_file().with_suffix(".tmp")
            tmp.write_text(json.dumps(live), encoding="utf-8")
            tmp.replace(prices_file())
        except OSError:
            pass
        return live
    return {**live, **saved}


@functools.lru_cache(maxsize=None)
def price_of(model: str) -> dict | None:
    """US$ per token for a model id as it appears on events, from litellm's
    price table; None if the table does not know it."""
    table = price_table()
    for key in (model, f"vertex_ai/{model}", f"gemini/{model}", f"bedrock/{model}",
                model.split("/")[-1], model.rsplit("-", 1)[0]):
        entry = table.get(key)
        if entry and entry.get("input_cost_per_token") is not None:
            return entry
    return None


def cost_usd(model: str, prompt: int, cached: int, output: int) -> float | None:
    """What one call costs at list price. Cached prompt tokens are inside
    `prompt` and billed at the cache-read rate; thinking is inside `output`."""
    p = price_of(model)
    if p is None:
        return None
    tier = "_above_200k_tokens" if prompt > _TIER else ""
    rate = lambda name, fallback: p.get(name + tier) or p.get(name) or fallback  # noqa: E731
    inp = rate("input_cost_per_token", 0.0)
    return ((prompt - cached) * inp
            + cached * rate("cache_read_input_token_cost", inp)
            + output * rate("output_cost_per_token", 0.0))


def summary(days: int = 30, db=None) -> dict:
    """Every model call in the window, bucketed by model, source, and day.

    `db` overrides the session store (tests point it at a fixture file); the
    default is the one in runtime/sessions.
    """
    import json

    since = time.time() - days * 86400
    by_model: dict[str, dict] = defaultdict(_blank)
    by_source: dict[str, dict] = defaultdict(_blank)
    by_day: dict[str, dict] = defaultdict(_blank)
    totals = _blank()
    unpriced: set[str] = set()

    from meeseek.runtime import history

    for r in history.rows(since=since, db=Path(db) if db else None):
        ts, e = r.at, r.event
        um = e.get("usage_metadata")
        if not um:
            continue
        prompt = int(um.get("prompt_token_count") or 0)
        # thinking tokens are billed as output
        output = int(um.get("candidates_token_count") or 0) + int(
            um.get("thoughts_token_count") or 0)
        total = int(um.get("total_token_count") or (prompt + output))
        # already inside prompt_token_count; kept for the dashboard's hit rate
        cached = int(um.get("cached_content_token_count") or 0)
        model = e.get("model_version") or "unknown"
        cost = cost_usd(model, prompt, cached, output)
        if cost is None:
            unpriced.add(model)
            cost = 0.0
        for bucket, key in ((by_model, model),
                            (by_source, e.get("author") or "agent"),
                            (by_day, _day(ts))):
            _add(bucket[key], prompt, output, cached, total, cost)
        _add(totals, prompt, output, cached, total, cost)

    # Claude Code's turns are not meeseek's events; runtime/claude_agent.py logs each.
    # They run on this machine's Claude Code subscription, so they count as
    # requests and tokens at US$0 — Claude Code's API-price estimate is not a bill.
    from meeseek.runtime.claude_agent import usage_log

    try:
        lines = usage_log().read_text(encoding="utf-8").splitlines() if usage_log().exists() else []
    except OSError:
        lines = []
    for raw in lines:
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("ts", 0) < since:
            continue
        prompt, output, cached = int(r["prompt"]), int(r["output"]), int(r["cached"])
        for bucket, key in ((by_model, r.get("model") or "claude"),
                            (by_source, r.get("source") or "claude"), (by_day, _day(r["ts"]))):
            _add(bucket[key], prompt, output, cached, prompt + output, 0.0)
        _add(totals, prompt, output, cached, prompt + output, 0.0)

    # Direct model calls (DIRECT_LOG), at list price like the turns'.
    try:
        lines = direct_log().read_text(encoding="utf-8").splitlines() if direct_log().exists() else []
    except OSError:
        lines = []
    for raw in lines:
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("ts", 0) < since:
            continue
        prompt, output, cached = int(r["prompt"]), int(r["output"]), int(r["cached"])
        model = r.get("model") or "unknown"
        cost = cost_usd(model, prompt, cached, output)
        if cost is None:
            unpriced.add(model)
            cost = 0.0
        for bucket, key in ((by_model, model), (by_source, r.get("source") or "direct"),
                            (by_day, _day(r["ts"]))):
            _add(bucket[key], prompt, output, cached, prompt + output, cost)
        _add(totals, prompt, output, cached, prompt + output, cost)

    def rows_of(bucket: dict, key: str) -> list[dict]:
        out = [{key: k, **v} for k, v in bucket.items()]
        out.sort(key=lambda r: r["requests"], reverse=True)
        return out

    from meeseek.config import cfg

    return {
        "days": days,
        "usd_to_twd": cfg.usage.usd_to_twd,
        "unpriced": sorted(unpriced),  # models whose calls count as US$0: not in the price table
        "totals": totals,
        "by_model": rows_of(by_model, "model"),
        "by_source": rows_of(by_source, "source"),
        "by_day": sorted(({"day": d, **v} for d, v in by_day.items()),
                         key=lambda r: r["day"]),
    }
