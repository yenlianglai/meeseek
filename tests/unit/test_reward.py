"""A turn's reward: what a person did outranks a detector, a judge, the harness's marks; silence is not success."""

import time

from meeseek.runtime import feedback, reward, trajectory


def _turn(inv="i1", skill="sprint", at=None, **auto):
    base = dict.fromkeys(["tool_errors", "cap_hits", "grace", "consent_held", "reconcile", "appended",
                          "empty_nudge", "model_error"], 0)
    return {"inv": inv, "skill": skill, "at": at or time.time(), "auto": {**base, **auto}, "request": "x",
            "policy": {"bundle": "b1"}, "cost_usd": 0.05, "latency_s": 10}


def test_a_person_outranks_everything_else():
    r = reward.reward(_turn(reconcile=1), [{"kind": "thumb", "value": 1}, {"kind": "correction"}])
    assert (r.value, r.tier, r.confidence) == (1.0, "explicit", 1.0)
    r = reward.reward(_turn(), [{"kind": "write_edited"}, {"kind": "write_rejected"}])
    assert r.value == -0.3 and r.tier == "explicit"


def test_a_correction_outranks_the_judge_and_the_harness_marks():
    r = reward.reward(_turn(appended=1), [{"kind": "correction"}, {"kind": "judge", "score": 5}])
    assert r.tier == "implicit" and r.value == -0.6 + 0.25 * -0.5


def test_the_judge_counts_only_while_it_is_trusted():
    assert reward.reward(_turn(), [{"kind": "judge", "score": 5}]).value == 1.0
    assert reward.reward(_turn(), [{"kind": "judge", "score": 5}], judge_trusted=False).tier == "none"


def test_the_harness_marks_alone_and_silence():
    r = reward.reward(_turn(tool_errors=5, grace=1), [])
    assert r.tier == "automatic" and r.value == -0.3 + -0.4
    assert reward.reward(_turn(), []).value is None                        # nothing said: not a success


def test_an_infrastructure_failure_is_not_the_policys():
    r = reward.reward(_turn(model_error=1), [])
    assert r.infra and r.value is None
    assert reward.reward({**_turn(), "retries": 2}, [{"kind": "thumb", "value": 1}]).infra


def test_a_misfiled_dismissal_counts_and_another_reason_does_not():
    assert reward.reward(_turn(), [{"kind": "dismissed", "reason": "分類錯"}]).value == -0.5
    assert reward.reward(_turn(), [{"kind": "dismissed", "reason": "已處理"}]).value is None


def test_the_judge_is_trusted_only_while_it_agrees_with_people():
    for i in range(5):
        feedback.record(f"i{i}", "s", "thumb", value=-1)
        feedback.record(f"i{i}", "s", "judge", score=5)
    assert not reward.judge_trusted()


def test_the_scoreboard_per_skill_and_compaction_keep_the_totals():
    now = time.time()
    old = now - (trajectory.RETAIN_DAYS + 3) * 86400
    for i, at in enumerate([old, old, now]):
        trajectory.turns_store().append(_turn(inv=f"t{i}", at=at))
        feedback.record(f"t{i}", "s", "thumb", value=1 if i else -1)
    before = reward.scoreboard(days=90)
    assert reward.compact(now) == 2
    assert {r["inv"] for r in trajectory.turns_store().read()} == {"t2"}            # the old ones folded and gone
    after = reward.scoreboard(days=90)
    assert after == before and after[0]["turns"] == 3 and after[0]["thumbs"] == [2, 1]


def test_judging_books_against_the_budget_and_records_the_score(monkeypatch):
    trajectory.turns_store().append(_turn(inv="u1"))
    monkeypatch.setattr(reward, "judge", lambda t: {"score": 2, "tags": ["missed_step"], "why": "漏查"})
    assert reward.judge_unlabeled(limit=5, calibrate=0, spend=lambda usd: False) == 0
    assert reward.judge_unlabeled(limit=5, calibrate=0) == 1
    [o] = [o for o in feedback.read() if o["kind"] == "judge"]
    assert o["score"] == 2 and o["tags"] == ["missed_step"]


def test_a_turns_latest_thumb_is_the_one_that_counts():
    r = reward.reward(_turn(), [{"kind": "thumb", "value": 1}, {"kind": "thumb", "value": -1, "note": "錯了"}])
    assert r.value == -1.0
    for kind_value in (1, -1, -1):
        feedback.record("t1", "s", "thumb", value=kind_value)
    trajectory.turns_store().append(_turn(inv="t1"))
    assert reward.scoreboard(days=7)[0]["thumbs"] == [0, 1]
