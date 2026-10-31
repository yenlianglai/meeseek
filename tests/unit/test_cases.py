"""The case library: graded turns as situations, deduped, capped, retired — and replayed with their context."""

import json
import sqlite3
import time

from meeseek.runtime import cases, reward
from meeseek.runtime.reward import Reward
from meeseek.config.paths import home


def _store(tmp_path, monkeypatch, rows):

    db = home().sessions_db
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE events (id TEXT, app_name TEXT, user_id TEXT, session_id TEXT, invocation_id TEXT,"
                 " timestamp REAL, event_data TEXT)")
    for i, (inv, author, text) in enumerate(rows):
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)",
                     (f"e{i}", "agents", "webui", "s1", inv, 100.0 + i,
                      json.dumps({"id": f"e{i}", "invocation_id": inv, "author": author,
                                  "content": {"role": "user" if author == "user" else "model",
                                              "parts": [{"text": text}]}})))
    conn.commit()


def _turn(inv, request="幫我開一張 Story 給 ACME-17893", skill="sprint"):
    return {"inv": inv, "session": "s1", "skill": skill, "request": request, "cost_usd": 0.05, "reply_head": "好"}


def test_a_turn_becomes_a_case_with_the_conversation_before_it(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, [("i0", "user", "先看 ACME-17893"), ("i0", "sprint_agent", "在 Code Review"),
                                   ("i1", "user", "幫我開一張 Story 給 ACME-17893"), ("i1", "sprint_agent", "開好了"),
                                   ("i2", "user", "不對")])
    assert cases.add(_turn("i1"), Reward(-1.0, 1.0, "explicit"), "negative",
                     [{"kind": "thumb", "value": -1, "note": "清單前要空一行"}]) == 1
    [c] = cases.load("sprint")
    assert c["target_turn"] == "幫我開一張 Story 給 ACME-17893" and len(c["prefix_events"]) == 2   # not the turn, not after
    assert c["status"] == "confirmed" and "清單前要空一行" in c["reference"]


def test_a_rewording_is_the_same_case_seen_again(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, [("i1", "user", "幫我開一張 Story 給 ACME-17893"),
                                   ("i2", "user", "幫我開一張 Story 給 ACME-17893 吧")])
    cases.add(_turn("i1"), Reward(-0.6, 0.6, "implicit"), "negative", [])
    cases.add(_turn("i2", "幫我開一張 Story 給 ACME-17893 吧"), Reward(-0.6, 0.6, "implicit"), "negative", [])
    [c] = cases.load("sprint")
    assert c["seen"] == 2 and c["status"] == "pattern"


def test_each_kind_is_capped_and_guesses_go_before_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(cases, "MAX_CASES", 2)
    now = time.time()
    for i, status in enumerate(["confirmed", "guess", "pattern"]):
        cases.save({"id": f"c{i}", "skill": "sprint", "kind": "negative", "status": status, "last_seen": now + i,
                    "seen": 1, "confirmed": status == "confirmed", "request": f"r{i}"})
    assert cases.evict("sprint") == 1
    assert sorted(c["id"] for c in cases.load("sprint")) == ["c0", "c2"]


def test_a_solved_negative_turns_positive_and_a_flaky_one_retires():
    c = {"id": "n1", "skill": "sprint", "kind": "negative", "status": "guess", "seen": 1, "results": []}
    cases.save(c)
    cases.record_result(c, run="r1", bundle="b", outcome="pass")
    assert cases.record_result(c, run="r2", bundle="b", outcome="pass")["kind"] == "positive"
    f = {"id": "f1", "skill": "sprint", "kind": "negative", "status": "guess", "seen": 1, "results": []}
    cases.record_result(f, run="r1", bundle="b", outcome="error")
    assert cases.record_result(f, run="r2", bundle="b", outcome="error")["status"] == "retired"


def test_a_slice_keeps_to_the_budget_and_old_cases_retire():
    now = time.time()
    for i, cost in enumerate([0.05, 0.05, 0.5]):
        cases.save({"id": f"s{i}", "skill": "code", "kind": "negative", "status": "guess", "last_seen": now,
                    "seen": 1, "confirmed": False, "request": f"r{i}", "cost_hint_usd": cost})
    assert [c["id"] for c in cases.slice("code", kind="negative", n=5, budget_usd=0.2)] == ["s0", "s1"]
    assert cases.retire(now=now + 31 * 86400) == 3                       # guesses not seen again in a month


def test_runs_are_indexed_and_compared():
    cases.record_run({"run_id": "a", "kind": "probe", "cases": {"x": "pass", "y": "fail"}})
    cases.record_run({"run_id": "b", "kind": "probe", "cases": {"x": "pass", "y": "pass"}})
    assert cases.diff("a", "b") == {"y": {"a": "fail", "b": "pass"}}


def test_mint_takes_only_confident_graded_turns(monkeypatch):
    added = []
    monkeypatch.setattr(reward, "rewards", lambda days: [(_turn("a"), Reward(-1.0, 1.0, "explicit")),
                                                         (_turn("b"), Reward(-0.3, 0.5, "automatic")),
                                                         (_turn("c"), Reward(0.6, 1.0, "explicit")),
                                                         (_turn("d"), Reward(None, 0.0, "none"))])
    monkeypatch.setattr(cases, "add", lambda turn, r, kind, outs: added.append((turn["inv"], kind)) or 1)
    assert cases.mint() == 2 and added == [("a", "negative"), ("c", "positive")]
