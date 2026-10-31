"""Weekly rule review: each learned rule is replayed with and without it, the
pair of judgements says whether the rule is doing its job, and two reviews
that agree act on it."""

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from meeseek.runtime import learned_rules as lr
from meeseek.runtime import rule_review as review
from meeseek.runtime.replays import Replay
from dataclasses import replace

from meeseek.config.paths import home, set_home

RULE = "開 Jira 票時標題一律用純中文摘要，類型由 issue type 欄位表達"


class TestVerdict:
    @pytest.mark.parametrize("w,wo,verdict", [
        (True, False, "working"), (True, True, "redundant"),
        (False, False, "not_followed"), (None, False, "unknown")])
    def test_the_pair_decides(self, w, wo, verdict):
        assert review.verdict_of({"follows": w}, {"follows": wo}) == verdict


class TestReview:
    @pytest.fixture
    def rule_with_case(self, tmp_path, monkeypatch):
        skills = tmp_path / "skills"
        (skills / "sprint").mkdir(parents=True)
        (skills / "sprint" / "SKILL.md").write_text("---\ndescription: d\n---\nbody")
        set_home(replace(home(), skills=skills))
        monkeypatch.setattr(lr, "_learned_path", lambda s: tmp_path / f"{s}.learned.md")
        lr.write_learned_rule("sprint", RULE, "r", "in-session")
        case_dir = lr.cases_dir() / "sprint"
        case_dir.mkdir(parents=True)
        (case_dir / f"{lr.rule_id(RULE)}.json").write_text(
            '{"turns": [{"user_content": {"parts": [{"text": "幫我開一張 reranker 的 Story"}]}}]}')
        return tmp_path

    def test_replayed_with_and_without_the_rule(self, rule_with_case, monkeypatch):
        seen = []

        async def fake_replay(prompt, *, exclude_rule=""):
            seen.append((prompt, exclude_rule))
            return Replay(reply="[Acme App] 修正排序" if not exclude_rule else "[Acme App] Story: fix ranking")

        monkeypatch.setattr("meeseek.runtime.replays.replay", fake_replay)
        monkeypatch.setattr(review, "judge",
                            lambda rule, prompt, reply, calls: {"follows": "Story" not in reply, "why": "x"})
        [out] = asyncio.run(review.run())["rules"]
        assert seen == [("幫我開一張 reranker 的 Story", ""), ("幫我開一張 reranker 的 Story", lr.rule_id(RULE))]
        assert out["verdict"] == "working"

    def test_a_rule_without_a_case_is_marked(self, rule_with_case):
        (lr.cases_dir() / "sprint" / f"{lr.rule_id(RULE)}.json").unlink()
        [out] = asyncio.run(review.run())["rules"]
        assert out["verdict"] == "no_case"


class TestExcluding:
    def test_an_excluded_rule_leaves_memory_for_that_context_only(self, tmp_path, monkeypatch):
        skills = tmp_path / "skills"
        (skills / "sprint").mkdir(parents=True)
        (skills / "sprint" / "SKILL.md").write_text("---\ndescription: d\n---\nbody")
        set_home(replace(home(), skills=skills))
        monkeypatch.setattr(lr, "_learned_path", lambda s: tmp_path / f"{s}.learned.md")
        lr.write_learned_rule("sprint", RULE, "r", "in-session")
        with lr.excluding(lr.rule_id(RULE)):
            assert lr.load_learned_rules("sprint") == ""
        assert RULE in lr.load_learned_rules("sprint")


class TestLock:
    def test_a_stale_lock_does_not_block(self, tmp_path, monkeypatch):
        review.lock_file().write_text(datetime.now(timezone.utc).isoformat())
        assert review.load()["running"] is True
        review.lock_file().write_text((datetime.now(timezone.utc) - timedelta(hours=4)).isoformat())
        assert review.load()["running"] is False


class TestTimeouts:
    def test_a_replay_that_never_returns_is_cut_off(self, monkeypatch, tmp_path):
        skills = tmp_path / "skills"
        (skills / "sprint").mkdir(parents=True)
        (skills / "sprint" / "SKILL.md").write_text("---\ndescription: d\n---\nbody")
        set_home(replace(home(), skills=skills))
        monkeypatch.setattr(lr, "_learned_path", lambda s: tmp_path / f"{s}.learned.md")
        monkeypatch.setattr(review, "_REPLAY_TIMEOUT_S", 0.05)
        lr.write_learned_rule("sprint", RULE, "r", "in-session")
        (lr.cases_dir() / "sprint").mkdir(parents=True)
        (lr.cases_dir() / "sprint" / f"{lr.rule_id(RULE)}.json").write_text(
            '{"turns": [{"user_content": {"parts": [{"text": "開一張單"}]}}]}')

        async def hangs(prompt, *, exclude_rule=""):
            await asyncio.sleep(10)

        monkeypatch.setattr("meeseek.runtime.replays.replay", hangs)
        [out] = asyncio.run(review.run())["rules"]
        assert out["verdict"] == "unknown" and "timed out" in out["with"]["why"]


class TestActions:
    TODAY = date(2026, 10, 12)

    @pytest.mark.parametrize("verdict,previous,action", [
        ("redundant", "redundant", "remove"), ("not_followed", "not_followed", "remove"),
        ("working", "working", "graduate"),
        ("redundant", None, ""), ("redundant", "working", ""), ("not_followed", "redundant", ""),
        ("working", None, ""), ("unknown", "unknown", "")])
    def test_two_reviews_must_agree(self, verdict, previous, action):
        assert review.action_for(verdict, previous, "2026-10-01", self.TODAY) == action

    def test_a_rule_without_a_case_goes_stale(self):
        assert review.action_for("no_case", None, "2026-10-01", self.TODAY) == ""
        assert review.action_for("no_case", None, "2026-09-01", self.TODAY) == "stale"
        assert review.action_for("no_case", None, "", self.TODAY) == "stale"


class TestRemoval:
    @pytest.fixture
    def redundant_rule(self, tmp_path, monkeypatch):
        skills = tmp_path / "skills"
        (skills / "sprint").mkdir(parents=True)
        (skills / "sprint" / "SKILL.md").write_text("---\ndescription: d\n---\nbody")
        set_home(replace(home(), skills=skills))
        monkeypatch.setattr(lr, "_learned_path", lambda s: tmp_path / f"{s}.learned.md")
        lr.write_learned_rule("sprint", RULE, "r", "in-session")
        (lr.cases_dir() / "sprint").mkdir(parents=True)
        (lr.cases_dir() / "sprint" / f"{lr.rule_id(RULE)}.json").write_text(
            '{"turns": [{"user_content": {"parts": [{"text": "開一張單"}]}}]}')

        async def fake_replay(prompt, *, exclude_rule=""):
            return Replay(reply="ok")

        monkeypatch.setattr("meeseek.runtime.replays.replay", fake_replay)
        monkeypatch.setattr(review, "judge", lambda *a: {"follows": True, "why": "x"})

    def test_removed_on_the_second_agreeing_review_and_restorable(self, redundant_rule):
        first = asyncio.run(review.run())
        assert first["actions"] == [] and lr.count_rules("sprint") == 1
        second = asyncio.run(review.run())
        assert [a["action"] for a in second["actions"]] == ["remove"]
        assert lr.count_rules("sprint") == 0
        [archived] = review.archived()
        assert archived["text"] == RULE and "SOP" in archived["why"]
        assert review.load()["latest"]["actions"] == second["actions"]

        review.restore("sprint", archived["id"])
        assert [r.text for r in lr.parse_learned("sprint")] == [RULE]
        assert review.archived() == []

    def test_restoring_something_not_archived_is_an_error(self, redundant_rule):
        with pytest.raises(ValueError):
            review.restore("sprint", "nope")


class TestAtCapacity:
    def test_the_refusal_lists_the_rules_to_choose_from(self, tmp_path, monkeypatch):
        skills = tmp_path / "skills"
        (skills / "sprint").mkdir(parents=True)
        (skills / "sprint" / "SKILL.md").write_text("---\ndescription: d\n---\nbody")
        set_home(replace(home(), skills=skills))
        monkeypatch.setattr(lr, "_learned_path", lambda s: tmp_path / f"{s}.learned.md")
        monkeypatch.setattr(lr, "MAX_RULES", 1)
        lr.write_learned_rule("sprint", RULE, "r", "in-session")
        with pytest.raises(ValueError) as e:
            lr.write_learned_rule("sprint", "查 log 之前先確認是哪個環境的叢集與 namespace", "r", "in-session")
        assert RULE in str(e.value) and "forget_sop_rule" in str(e.value)


class TestLibraryCase:
    """A rule replayed in the conversation it came from, with it and with a policy that leaves it out."""

    def _rule(self, cue=""):
        from meeseek.runtime import learned_rules as lr

        return lr.LearnedRule(text="開 Jira 單時標題一律用中文", date="2026-10-21", source="in-session",
                              reason="r", raw="- 開 Jira 單時標題一律用中文 _（2026-10-21，in-session，依據：r）_", cue=cue)

    def test_its_own_case_first_then_one_matching_its_cue(self):
        from meeseek.runtime import cases

        cases.save({"id": "c1", "skill": "sprint", "origin": "turn", "status": "guess", "target_turn": "幫我開 Jira 單",
                    "request": "幫我開 Jira 單給 ACME-1", "kind": "negative"})
        rule = self._rule()
        assert review.library_case("sprint", rule) is None                       # no own case, no cue
        assert review.library_case("sprint", self._rule(cue="開 Jira 單"))["id"] == "c1"
        cases.save({"id": "own", "skill": "sprint", "origin": f"rule:{rule.id}", "status": "confirmed",
                    "target_turn": "開單", "request": "開單", "kind": "negative"})
        assert review.library_case("sprint", self._rule(cue="開 Jira 單"))["id"] == "own"

    def test_replayed_with_and_without_the_rule_in_its_conversation(self, monkeypatch):
        from meeseek.runtime import cases, policy, replays
        from meeseek.runtime.replays import SessionRun, TurnRun

        rule = self._rule()
        cases.save({"id": "own", "skill": "sprint", "origin": f"rule:{rule.id}", "status": "confirmed",
                    "target_turn": "開單", "request": "開單", "kind": "negative", "prefix_events": [{"id": "e0"}]})
        seen = []

        async def fake(turns, prefix_events=(), bundle=None, **k):
            seen.append((turns, len(prefix_events), bundle))
            return SessionRun(turns=[TurnRun(reply="中文標題" if bundle is None else "English title")])

        monkeypatch.setattr(replays, "run_session", fake)
        monkeypatch.setattr(review, "judge", lambda rule_text, prompt, reply, calls: {"follows": reply == "中文標題", "why": ""})
        monkeypatch.setattr("meeseek.runtime.learned_rules.parse_learned", lambda skill: [rule])
        out = asyncio.run(review.check("sprint", rule))
        assert out["verdict"] == "working" and out["case"] == "own"
        assert seen[0][2] is None and isinstance(seen[1][2], policy.PolicyBundle)
        assert seen[1][2].rules == {"sprint": ()} and seen[0][1] == 1      # the rule left out; its context seeded
