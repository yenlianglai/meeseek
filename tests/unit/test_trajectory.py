"""One record per turn, derived from its events: who answered, what it cost, the harness's marks on it."""

from meeseek.context import prompts
from meeseek.runtime import trajectory


def _ev(author, *parts, inv="i1", ts=100.0, usage=None, model=None):
    """An event in the shape both engines store one (history.record)."""
    e = {"author": author, "invocation_id": inv, "timestamp": ts,
         "content": {"role": "user" if author == "user" else "model", "parts": list(parts)}}
    if usage:
        e["usage_metadata"] = usage
    if model:
        e["model_version"] = model
    return e


def text(t):
    return {"text": t}


def call(name, args=None):
    return {"function_call": {"name": name, "args": args or {}}}


def resp(name, response):
    return {"function_response": {"name": name, "response": response}}


def test_who_answered_what_it_cost_and_what_was_asked():
    events = [
        _ev("user", text("ACME-17893 現在什麼狀態"), ts=100),
        _ev("acme_app_orchestrator", call("transfer_to_agent", {"agent_name": "sprint_agent"}), ts=101),
        _ev("sprint_agent", call("jira_get_issue"), ts=102, model="gemini-3.8-flash",
            usage={"prompt_token_count": 1000, "cached_content_token_count": 400, "candidates_token_count": 50}),
        _ev("sprint_agent", resp("jira_get_issue", {"result": "Code Review"}), ts=103),
        _ev("sprint_agent", text("在 Code Review"), ts=106),
        _ev("user", text("別的對話"), inv="i2", ts=200),
    ]
    rec = trajectory.summarize(events, "i1", root="acme_app_orchestrator")
    assert rec["skill"] == "sprint" and rec["request"] == "ACME-17893 現在什麼狀態"
    assert rec["calls"] == [["jira_get_issue", False]] and rec["reply_head"] == "在 Code Review"
    assert rec["tokens"] == {"prompt": 1000, "cached": 400, "output": 50}
    assert rec["cost_usd"] > 0 and rec["latency_s"] == 6
    assert not any(rec["auto"].values())


def test_the_harness_marks_are_found_by_their_texts():
    grace = prompts.text("runtime/callbacks#grace", budget=60)
    held = prompts.text("runtime/consent#ask_first", what="這條規則")
    nudge = prompts.text("runtime/callbacks#empty_reply")
    events = [
        _ev("user", text("幫我開單")),
        _ev("sprint_agent", resp("jira_create_issue", {"error": "boom", "tool_failed": True})),
        _ev("sprint_agent", resp("kb_grep", {"result": grace})),
        _ev("sprint_agent", resp("propose_sop_rule", {"result": held})),
        _ev("sprint_agent", call("reconcile")),
        _ev("user", text(nudge)),
        _ev("sprint_agent", text("好了\n\n⚠️ 系統紀錄：以下寫入沒有成功——")),
    ]
    auto = trajectory.summarize(events, "i1")["auto"]
    assert auto["tool_errors"] == 1 and auto["grace"] == 1 and auto["consent_held"] == 1
    assert auto["reconcile"] == 1 and auto["empty_nudge"] == 1 and auto["appended"] == 1


def test_a_model_error_reply_is_marked_and_a_router_only_turn_is_the_routers():
    err = prompts.text("runtime/callbacks#model_error", detail="400")
    rec = trajectory.summarize([_ev("user", text("hi")), _ev("acme_app_orchestrator", text(err))], "i1",
                               root="acme_app_orchestrator")
    assert rec["auto"]["model_error"] == 1 and rec["skill"] == "router"


def test_a_resumed_turn_keeps_its_last_record():
    trajectory.turns_store().append({"inv": "a", "n": 1})
    trajectory.turns_store().append({"inv": "a", "n": 2})
    trajectory.turns_store().append({"inv": "b", "n": 1})
    assert sorted((r["inv"], r["n"]) for r in trajectory.fold(trajectory.turns_store().read())) == [("a", 2), ("b", 1)]


def test_a_replay_is_never_recorded():
    from meeseek.runtime.replaying import _REPLAYING, replaying

    assert replaying("replay") and replaying("eval") and not replaying("webui")
    token = _REPLAYING.set(True)
    try:
        assert replaying("webui")
    finally:
        _REPLAYING.reset(token)


