"""Learned rules — 寫入路徑的 guardrail：品質門檻、來源、去重、上限。"""

from dataclasses import replace

import pytest

import meeseek.runtime.learned_rules as evo
from meeseek.config.paths import home, set_home
from meeseek.runtime.learned_rules import (
    MAX_RULES,
    _quality_gate,
    load_learned_rules,
    make_propose_tool,
    remove_learned_rule,
    write_learned_rule,
)


@pytest.fixture
def tmp_skill(tmp_path, monkeypatch):
    """隔離的 skills dir，含一個假 skill。"""
    skills = tmp_path / "skills"
    (skills / "demo").mkdir(parents=True)
    (skills / "demo" / "SKILL.md").write_text("---\ndescription: d\n---\nbody")
    set_home(replace(home(), skills=skills))
    return skills


class TestQualityGate:
    def test_old_garbage_is_rejected(self):
        """JSON 碎片、截斷、標點開頭、過短的字串都必須被擋。"""
        real_garbage = [
            '": "backend", "pod":',       # JSON 碎片（infra SOP 挖出來的）
            "使用 `kb_grep",              # 反引號截斷
            "16.1\"}]},\"severity",       # JSON 碎片
            ": `{\"name\": \"ai-",        # 標點開頭 + 截斷
            "太短",                        # 過短
        ]
        for g in real_garbage:
            assert _quality_gate(g) is not None, f"應拒絕: {g!r}"

    def test_good_rule_passes(self):
        assert _quality_gate("呼叫 get_file 前先用 search_code 確認路徑存在，避免 404 重試迴圈") is None


class TestWritePath:
    def test_the_bullet_carries_its_date_reason_and_source(self, tmp_skill):
        """The bullet IS the record: date, reason and source live on it, so the
        file itself shows whether two similar rules share an origin."""
        msg = write_learned_rule("demo", "查 log 前先確認環境對應的 gcp_project 再下 filter", "list_log_entries 空結果 42 次", "in-session")
        assert "1/10" in msg and "無需重啟" in msg
        [rule] = evo.parse_learned("demo")
        assert "gcp_project" in rule.text
        assert rule.source == "in-session"
        assert rule.date and rule.reason.startswith("list_log_entries")

    def test_a_bullet_written_before_sources_parses_as_legacy(self, tmp_skill):
        """Old files must keep working, and an unknown origin must read as
        unknown rather than be dressed up as a real source."""
        (tmp_skill / "demo" / "learned.md").write_text(
            "# 學習規則\n- 舊格式的規則內容夠長了 _（2026-01-01，依據：舊的理由）_\n",
            encoding="utf-8")
        [rule] = evo.parse_learned("demo")
        assert rule.source == "legacy"
        assert rule.text == "舊格式的規則內容夠長了" and rule.date == "2026-01-01"

    def test_a_source_other_than_in_session_is_refused_before_anything_is_written(self, tmp_skill):
        """新規則只收 in-session（使用者在對話中同意）。其他來源——包括既有檔案裡
        出現的標籤——一律擋在門口。"""
        for bad in ("signal-aggregate", "turn-fact", "user-correction", "maintain-review",
                    "legacy", "explicit", "test"):
            with pytest.raises(ValueError, match="不是學習規則的來源"):
                write_learned_rule("demo", "一條看起來完全正常的規則內容夠長了", "r", bad)
        assert not (tmp_skill / "demo" / "learned.md").exists()

    def test_bullets_written_under_older_source_tags_still_parse_and_load(self, tmp_skill):
        """Only new writes are restricted; rules already on disk keep working."""
        (tmp_skill / "demo" / "learned.md").write_text(
            "# 學習規則\n"
            "- 使用者糾正後寫入的一條規則內容 _（2026-08-01，user-correction，依據：a）_\n"
            "- 維護審核時寫入的一條規則內容 _（2026-08-02，maintain-review，依據：b）_\n"
            "- 遷移過來的一條規則內容在這裡 _（2026-08-03，legacy，依據：c）_\n",
            encoding="utf-8")
        rules = evo.parse_learned("demo")
        assert [r.source for r in rules] == ["user-correction", "maintain-review", "legacy"]
        assert rules[0].text == "使用者糾正後寫入的一條規則內容" and rules[1].reason == "b"
        assert load_learned_rules("demo").count("\n- ") == 2
        write_learned_rule("demo", "新寫入的規則與舊條目並存不互相影響", "r", "in-session")
        assert evo.count_rules("demo") == 4

    def test_unknown_skill_rejected_with_available_list(self, tmp_skill):
        with pytest.raises(ValueError, match="可用"):
            write_learned_rule("nope", "一條看起來完全正常的規則內容夠長了", "r", "in-session")

    def test_cap_at_max_rules(self, tmp_skill):
        topics = ["查 log 前先確認環境", "開單標題一律純中文", "review 前先讀 AC", "留言前先讀既有討論",
                  "部署前先看 pipeline", "回報數字要附查詢語法", "改設定前先備份原值", "引用文件要附更新日期",
                  "排查前先建立可重現的訊號", "畫圖前先查知識庫架構"]
        for i, topic in enumerate(topics[:MAX_RULES]):
            write_learned_rule("demo", f"{topic}，這是第 {i} 條合格長度的規則內容", "r", "in-session")
        with pytest.raises(ValueError, match="上限"):
            write_learned_rule("demo", "第十一條合格長度的規則應該要被上限擋下來", "r", "in-session")

    def test_remove_by_keyword(self, tmp_skill):
        write_learned_rule("demo", "含有特殊關鍵字XYZZY的一條合格規則內容", "r", "in-session")
        assert "已移除 1 條" in remove_learned_rule("demo", "XYZZY")
        assert "XYZZY" not in load_learned_rules("demo")

    def test_loaded_rules_are_bullets_only(self, tmp_skill):
        write_learned_rule("demo", "載入時只回傳規則條目而不帶檔頭的一條規則", "r", "in-session")
        loaded = load_learned_rules("demo")
        assert loaded.startswith("- ") and "#" not in loaded.splitlines()[0]

    def test_skill_bound_tool_cannot_write_others(self, tmp_skill):
        tool = make_propose_tool("demo")
        assert "demo" in tool("綁定工具只能寫自己 skill 的規則這一條", "r")


class TestProposeTool:
    def test_one_propose_factory_in_two_shapes(self):
        import inspect

        # tool_context is injected by ADK and never reaches the declaration
        assert list(inspect.signature(make_propose_tool(None)).parameters) == ["skill", "rule", "reason", "cue",
                                                                               "tool_context"]
        assert list(inspect.signature(make_propose_tool("code")).parameters) == ["rule", "reason", "cue", "tool_context"]

    def test_both_shapes_write_in_session(self, tmp_skill):
        make_propose_tool(None)("demo", "協調者代某個 skill 寫入的一條規則內容", "r")
        make_propose_tool("demo")("子 agent 自己寫入的一條規則內容在這裡", "r")
        assert {r.source for r in evo.parse_learned("demo")} == {"in-session"}


class TestDynamicInjection:
    def test_rule_takes_effect_without_restart(self):
        """寫入 learned.md 後，agent 的動態 instruction 立即讀到（免重啟核心保證）。

        The test's own copy of the skills: the agent is built from the real
        SKILL.md, and the rule lands beside it, not in the repo."""
        from meeseek import engine
        from meeseek.core.turn import TurnState
        from meeseek.runtime.skills import discover_skills

        skills = {s.name: s for s in discover_skills()}
        agent = engine.skill_agent(skills["research"])
        t = TurnState(session="s", turn="t", watched=True, started=0.0, history=[], agent=agent.name)
        base = agent.system(t)
        write_learned_rule("research", "引用 KB 內容時一律附出處檔名這條測試規則", "r", "in-session")
        after = agent.system(t)
        assert "測試規則" in after and after != base

    def test_every_agent_skill_has_propose_tool(self):
        from meeseek import engine
        from meeseek.runtime.skills import discover_skills

        for s in discover_skills():
            names = [t.name for t in engine.skill_agent(s).tools]
            assert "propose_sop_rule" in names, f"{s.name} missing propose_sop_rule"


DIFF_RULE = "Review 時若要主張某段 code 會 crash，必須先在 sandbox 實跑取得證據才能留 comment"


class TestDuplicates:
    def test_a_reworded_rule_is_refused(self, tmp_skill):
        write_learned_rule("demo", DIFF_RULE, "r", "in-session")
        with pytest.raises(ValueError, match="已經有了"):
            write_learned_rule("demo", "review 時若要主張某段 code 會 crash，必須先在 sandbox 實際執行取得證據才可留 comment",
                               "r", "in-session")

    def test_a_rule_the_sop_already_says_is_refused(self, tmp_skill):
        (tmp_skill / "demo" / "SKILL.md").write_text(f"---\ndescription: d\n---\n- {DIFF_RULE}\n")
        with pytest.raises(ValueError, match="已經有了"):
            write_learned_rule("demo", DIFF_RULE + "。", "r", "in-session")

    def test_a_different_rule_is_written(self, tmp_skill):
        write_learned_rule("demo", DIFF_RULE, "r", "in-session")
        write_learned_rule("demo", "開 Jira 票時標題一律用純中文摘要，類型由 issue type 欄位表達", "r", "in-session")
        assert len(evo.parse_learned("demo")) == 2


class TestCase:
    def test_the_turns_before_the_rule_are_kept(self, tmp_skill, tmp_path, monkeypatch):
        from types import SimpleNamespace

        def turn(inv, user, reply):   # events in the shape the engine hands a tool (history.record)
            return [{"author": "user", "invocation_id": inv, "content": {"parts": [{"text": user}]}},
                    {"author": "code_agent", "invocation_id": inv, "content": {"parts": [{"text": reply}]}}]

        session = SimpleNamespace(id="s1", user_id="webui", events=[
            *turn("i1", "MR !165 會 crash 嗎", "會，第 42 行 SyntaxError"),
            *turn("i2", "不對，你沒跑就下結論", "了解，要記成規則嗎？"),
            *turn("i3", "好，記下來", "")])
        write_learned_rule("demo", DIFF_RULE, "使用者糾正", "in-session",
                           session=session, invocation_id="i3")
        case = evo.load_case("demo", evo.rule_id(DIFF_RULE))
        users = [t["user_content"]["parts"][0]["text"] for t in case["turns"]]
        assert users == ["MR !165 會 crash 嗎", "不對，你沒跑就下結論"]
        assert case["rule"] == DIFF_RULE and case["session_id"] == "s1"


class TestGraduate:
    def test_the_rule_moves_into_the_sop_and_only_that_file_is_committed(self, tmp_path, monkeypatch):
        import subprocess

        repo = tmp_path / "repo"
        skills = repo / "skills"
        (skills / "demo").mkdir(parents=True)
        sop = skills / "demo" / "SKILL.md"
        sop.write_text("---\ndescription: d\n---\nbody\n")
        (repo / "other.txt").write_text("untouched")
        for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                    ["add", "."], ["commit", "-q", "-m", "init"]):
            subprocess.run(["git", "-C", str(repo), *cmd], check=True)
        (repo / "other.txt").write_text("dirty")   # must not ride along
        set_home(replace(home(), skills=skills))
        monkeypatch.setattr(evo, "_learned_path", lambda s: tmp_path / f"{s}.learned.md")

        write_learned_rule("demo", DIFF_RULE, "r", "in-session")
        sha = evo.graduate("demo", evo.rule_id(DIFF_RULE))

        assert DIFF_RULE in sop.read_text() and evo.GRADUATED_HEADING in sop.read_text()
        assert evo.parse_learned("demo") == []
        changed = subprocess.run(["git", "-C", str(repo), "show", "--name-only", "--format=", sha],
                                 capture_output=True, text=True, check=True).stdout.split()
        assert changed == ["skills/demo/SKILL.md"]


def test_a_rule_carries_the_situation_it_is_for(tmp_skill):
    make_propose_tool("demo")("開 Jira 單時標題一律用中文，不中英混雜", "使用者糾正", cue="開 Jira 單時")
    [r] = evo.parse_learned("demo")
    assert r.cue == "開 Jira 單時" and r.reason == "使用者糾正" and r.text.startswith("開 Jira 單時標題")
