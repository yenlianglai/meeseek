"""The weekly improvement run: candidate edits held to what may change, probed blind, proposed — and you decide."""

from dataclasses import replace
import time
from types import SimpleNamespace

import pytest

from meeseek.config.paths import home, set_home
from meeseek.runtime import improve
from meeseek.runtime.improve import Candidate, Edit
from meeseek.runtime.replays import TurnRun

TEXTS = {"skill": "## 開單格式\n- summary 用中文\n- 段落之間留空行", "rules": "",
         "prompt:agents/router": "ROUTER", "prompt:blocks/common": "COMMON"}


class TestValidate:
    def test_an_edit_inside_the_skill_body_within_its_growth(self):
        out, errors = improve.apply_edits("sprint", [Edit(target="skill", op="insert_after", anchor="- 段落之間留空行",
                                                          text="- 標題和清單之間空一行")], TEXTS)
        assert not errors and out["skill"].endswith("- 標題和清單之間空一行")

    @pytest.mark.parametrize("edit,why", [
        (Edit(target="frontmatter", op="replace", anchor="x", text="y"), "不能改"),
        (Edit(target="prompt:internal/turn_judge", op="replace", anchor="x", text="y"), "不能改"),
        (Edit(target="skill", op="replace", anchor="不存在的句子", text="y"), "anchor"),
        (Edit(target="prompt:blocks/common", op="insert_after", anchor="COMMON", text="更多每輪都送的字"), "不能變長"),
        (Edit(target="skill", op="insert_after", anchor="- 段落之間留空行", text="字" * 500), "最多淨增"),
        (Edit(target="rules", op="add", text="短"), "規則"),
    ])
    def test_what_may_not_change_or_grow_is_refused(self, edit, why):
        _, errors = improve.apply_edits("sprint", [edit], TEXTS)
        assert any(why in e for e in errors)

    def test_always_on_text_may_change_as_long_as_it_does_not_grow(self):
        _, errors = improve.apply_edits("sprint", [
            Edit(target="prompt:blocks/common", op="replace", anchor="COMMON", text="NOMMOC")], TEXTS)
        assert not errors


def test_the_blind_judge_must_agree_both_ways_round(monkeypatch):
    answers = iter(["B", "A",       # base=A, cand=B → cand; then cand=A → cand: agreed
                    "B", "B"])      # disagrees with itself: a tie
    monkeypatch.setattr(improve, "pair", lambda case, a, b, budget: next(answers))
    assert improve.blind({}, TurnRun(), TurnRun(), improve.Budget()) == "cand"
    assert improve.blind({}, TurnRun(), TurnRun(), improve.Budget()) == "tie"


def test_the_budget_stops_what_it_cannot_pay_for():
    b = improve.Budget(usd=0.05, replays=1)
    assert b.take("propose", 0.04) and not b.take("propose", 0.04)
    assert b.can(0.0, replays=1) and not b.can(0.0, replays=2)


@pytest.fixture
def week(monkeypatch):
    """A week with one bad cluster in sprint, two cases, and candidates that propose an edit."""
    from meeseek.runtime import cases, reward

    monkeypatch.setattr(reward, "judge_unlabeled", lambda **k: 0)
    monkeypatch.setattr(improve, "mine", lambda skill=None: [{"skill": "sprint", "turns": [{"inv": "i1", "request": "開單"}] * 2,
                                                              "tags": ["missed_step"], "cases": ["n1", "n2"]}])
    monkeypatch.setattr(improve, "current", lambda skill: dict(TEXTS))
    monkeypatch.setattr(improve, "propose", lambda skill, cluster, texts, kind, budget: Candidate(edits=[
        Edit(target="skill", op="insert_after", anchor="- 段落之間留空行", text=f"- 清單前空一行（{kind}）", why="清單不見")]))
    neg = [{"id": "n1", "target_turn": "開單", "kind": "negative"}, {"id": "n2", "target_turn": "開單2", "kind": "negative"}]
    monkeypatch.setattr(cases, "slice", lambda skill, kind, n, budget_usd, ids=None: neg if kind == "negative" else [])
    monkeypatch.setattr(cases, "record_result", lambda c, **k: c)
    replayed = SimpleNamespace(error="", turns=[TurnRun(reply="r")], cost_usd=0.05)
    monkeypatch.setattr(improve, "_replay", lambda case, bundle, budget: replayed)
    monkeypatch.setattr(improve, "reflect", lambda skill, edits, outcomes, budget: {"cue": "開單", "lesson": "清單前空一行",
                                                                                   "evidence": ["n1"], "confidence": "medium",
                                                                                   "verdicts": {}})


def test_a_week_that_finds_a_better_edit_proposes_it(week, monkeypatch):
    monkeypatch.setattr(improve, "blind", lambda case, base, cand, budget: "cand")
    report = improve.run()
    [p] = improve.proposals(status="pending")
    assert report["proposal"] == p["id"] and p["skill"] == "sprint"
    assert "清單前空一行" in p["diff"] and p["outcomes"] == {"n1": "cand", "n2": "cand"}
    assert improve.lessons()[0]["lesson"] == "清單前空一行"


def test_a_week_that_finds_nothing_better_keeps_as_is(week, monkeypatch):
    monkeypatch.setattr(improve, "blind", lambda case, base, cand, budget: "tie")
    report = improve.run()
    assert not improve.proposals() and any("keep as is" in s for s in report["steps"])


def test_a_dry_run_proposes_nothing(week, monkeypatch):
    monkeypatch.setattr(improve, "blind", lambda case, base, cand, budget: "cand")
    assert "proposal" in improve.run(dry_run=True) and not improve.proposals()


class TestDecide:
    def _proposal(self, before="BODY", after="BODY2"):
        p = {"id": "p1", "kind": "edit", "skill": "sprint", "created": time.time(), "summary": "s",
             "changed": ["skill"], "after": {"skill": after}, "base_hashes": {"skill": improve._sha8(before)},
             "diff": "", "outcomes": {"n1": "cand"}, "cost_usd": 0.5, "lesson": None, "run_id": "r"}
        improve.proposals_store().append(p)
        return p

    def test_rejecting_keeps_your_note_for_the_next_run(self):
        self._proposal()
        assert improve.decide("p1", False, note="太囉嗦")["status"] == "rejected"
        [p] = improve.proposals(status="rejected")
        assert p["note"] == "太囉嗦"
        with pytest.raises(ValueError):
            improve.decide("p1", True)

    def test_a_proposal_made_against_text_that_changed_since_is_stale(self, monkeypatch):
        self._proposal(before="BODY")
        monkeypatch.setattr(improve, "current", lambda skill: {**TEXTS, "skill": "EDITED BY HAND"})
        assert improve.decide("p1", True)["status"] == "stale"

    def test_approving_writes_the_body_not_the_frontmatter_commits_and_reloads(self, monkeypatch, tmp_path):
        skill = tmp_path / "sprint" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("---\ndescription: d\n---\nBODY\n", encoding="utf-8")
        monkeypatch.setattr(improve, "_skill_file", lambda s: skill)
        monkeypatch.setattr(improve, "current", lambda s: {**TEXTS, "skill": "BODY"})
        set_home(replace(home(), skills=tmp_path))
        ran, reloaded = [], []
        monkeypatch.setattr(improve.subprocess, "run", lambda cmd, **k: ran.append(cmd) or
                            type("P", (), {"stdout": "abc1234\n"})())
        monkeypatch.setattr("meeseek.runtime.policy.reload", lambda: reloaded.append(1))
        self._proposal(before="BODY", after="BODY2")
        out = improve.decide("p1", True)
        assert out["status"] == "approved" and out["commit"] == "abc1234" and reloaded
        assert skill.read_text(encoding="utf-8") == "---\ndescription: d\n---\nBODY2\n"
        assert ran[0][:4] == ["git", "-C", str(tmp_path), "commit"] and ran[0][-1] == str(skill.resolve())


def test_a_change_that_made_its_skill_worse_gets_a_rollback_proposed(monkeypatch):
    from meeseek.runtime import reward
    from meeseek.runtime.reward import Reward

    now = time.time()
    improve.proposals_store().append({"id": "p1", "kind": "edit", "skill": "sprint", "created": now - 20 * 86400, "summary": "s"})
    improve.decisions_store().append({"id": "p1", "decision": "approved", "at": now - 15 * 86400})
    turns = [({"skill": "sprint", "at": now - 20 * 86400 + i}, Reward(0.6, 1.0, "explicit")) for i in range(6)] + \
            [({"skill": "sprint", "at": now - 10 * 86400 + i}, Reward(-0.4, 1.0, "explicit")) for i in range(6)]
    monkeypatch.setattr(reward, "rewards", lambda days: turns)
    assert improve.monitor(now) == 1
    [rb] = [p for p in improve.proposals() if p["kind"] == "rollback"]
    assert rb["of"] == "p1" and rb["status"] == "pending"
    assert improve.monitor(now) == 0                       # one open rollback is enough


def test_proposals_fold_their_decisions_and_notices_and_expire():
    now = time.time()
    improve.proposals_store().append({"id": "a", "kind": "edit", "skill": "code", "created": now})
    improve.proposals_store().append({"id": "b", "kind": "edit", "skill": "code", "created": now - 20 * 86400})
    improve.announced("a")
    assert [p["id"] for p in improve.proposals(status="pending")] == ["a"]
    assert improve.unannounced() == [] and [p["id"] for p in improve.proposals(status="expired")] == ["b"]
