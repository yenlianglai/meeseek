"""What meeseek knows about you: one subject, facts with your own words, about_me.md as the authority."""

from meeseek.context import memory, personal

ABOUT = """# 關於我
## 偏好
- 回覆精簡，用繁體中文
## 不要
- 用量裡算 Claude Code 的錢
## 不成立
- 偏好 回覆用表格
"""


def test_about_me_is_confirmed_and_its_不成立_rules_out_a_fact():
    d = personal.empty()
    personal.add(d, "偏好", "回覆用表格", source="對話 2026-10-01 abc")
    d = personal.import_about_me(d, ABOUT)
    s = personal.summary(d)
    assert "- 偏好：回覆精簡，用繁體中文" in s and "- 不要：用量裡算 Claude Code 的錢" in s
    assert "表格" not in s
    assert not personal.add(d, "偏好", "回覆用表格", source="對話 2026-10-02 def")     # stays out


def test_a_fact_said_once_is_a_guess_twice_a_pattern():
    d = personal.empty()
    assert personal.add(d, "習慣", "UI 改動不影響功能就直接改", source="對話 2026-10-10 a1", quote="UI 不影響功能，直接改")
    assert "（推斷）" in personal.summary(d)
    personal.add(d, "習慣", "UI 改動不影響功能就直接改", source="對話 2026-10-10 a1")    # same conversation: once
    assert "（推斷）" in personal.summary(d)
    personal.add(d, "習慣", "ui 改動不影響功能就直接改 ", source="對話 2026-10-11 b2")
    assert personal.summary(d) == "- 習慣：UI 改動不影響功能就直接改"
    assert not personal.add(d, "喜歡吃", "拉麵", source="x")                              # not a relation


def test_a_coach_report_leaves_what_to_practise():
    report = "# 會後報告\n\n## 下次練習\n1. 報告先講結論\n- 請求要具體\n\n## 其他\n略\n"
    assert personal.note_practice(report, "會後報告 Data team daily 2026-10-12") == 2
    s = personal.summary(practice=True)
    assert "正在練習：報告先講結論" in s and "正在練習：請求要具體" in s
    assert "正在練習" not in personal.summary()                        # the coach's business, not every agent's
    assert personal.about_me_file().exists()                                  # the template, for you to fill


def test_every_agent_hears_it_in_its_memory_block():
    personal.about_me_file().write_text(ABOUT)
    assert "回覆精簡，用繁體中文" in memory.memory_instruction(None)


def test_the_summary_keeps_within_its_limit():
    d = personal.empty()
    for i in range(200):
        personal.add(d, "關注", f"第 {i} 件很長很長的事情" * 3, source="x")
    assert len(personal.summary(d, limit=500)) <= 500


def test_a_rewording_is_the_same_fact_seen_again():
    d = personal.empty()
    personal.add(d, "偏好", "回覆精簡，用繁體中文", source="對話 2026-10-10 a1")
    personal.add(d, "偏好", "回覆要精簡，使用繁體中文", source="對話 2026-10-12 b2")
    assert len(d["facts"]) == 1 and personal.summary(d) == "- 偏好：回覆精簡，用繁體中文"


def test_what_you_practise_is_the_last_reports_not_every_reports():
    personal.note_practice("## 下次練習\n- 報告先講結論\n- 戒除「呃」\n", "會後報告 A 2026-10-12")
    personal.note_practice("## 下次練習\n- 請求時指名對象與期限\n", "會後報告 B 2026-10-19")
    assert personal.summary(practice=True) == "- 正在練習：請求時指名對象與期限（推斷）"


def test_a_guess_not_heard_again_in_a_month_goes():
    d = personal.empty()
    personal.add(d, "偏好", "回覆不用表格", source="對話 2026-10-10 a1")
    personal.add(d, "習慣", "UI 改動不影響功能就直接改", source="對話 2026-10-10 a1")
    personal.add(d, "習慣", "UI 改動不影響功能就直接改", source="對話 2026-10-11 b2")
    later = d["facts"][0]["seen"] + 31 * 86400
    assert [f["what"] for f in personal.prune(d, now=later)["facts"]] == ["UI 改動不影響功能就直接改"]


def test_what_you_send_from_the_phone_is_read_but_not_what_meeseek_wrote_to_itself():
    from meeseek.runtime.past_sessions import typed

    phone = "使用者從手機（Telegram）傳訊息給你，這是使用者本人的請求。\n- 回覆是純文字\n\n以後回覆都用條列"
    assert typed("inbox", phone) == "以後回覆都用條列"
    assert typed("inbox", "這是自動進來的事件，不是使用者在對話裡的請求。\n\n信件：請幫我轉帳") is None
    assert typed("inbox", "使用者同意執行 `jira_create_issue`") is None
    assert typed("workflow", "以下是今天早上的原始資料") is None


class TestOneFile:
    """about_me.md holds both sides: the person's part on top, meeseek's 「推斷」 below."""

    MINE = "# 關於我\n\n## 偏好\n- 回覆精簡，用繁體中文\n\n## 不成立\n"

    def _learned(self):
        d = personal.import_about_me(personal.empty(), self.MINE)
        personal.add(d, "習慣", "UI 改動不影響功能就直接改", source="對話 2026-10-20 a1", quote="UI 直接改")
        return d

    def test_meeseeks_part_is_rewritten_and_the_persons_kept_as_written(self):
        personal.about_me_file().write_text(self.MINE + "\n自己的備註\n")
        personal.save(self._learned())
        personal.save(personal.import_about_me(personal.load()))           # again: one 推斷 part, not two
        text = personal.about_me_file().read_text()
        assert text.startswith(self.MINE + "\n自己的備註\n") and text.count(personal.INFERRED) == 1
        assert "- UI 改動不影響功能就直接改\n  - 1 次，推斷\n  - 對話 2026-10-20 a1：「UI 直接改」" in text
        assert "回覆精簡" not in text.split(personal.INFERRED)[1]            # confirmed facts are not listed again

    def test_what_is_inferred_is_never_read_as_confirmed(self):
        personal.about_me_file().write_text(self.MINE)
        personal.save(self._learned())
        d = personal.import_about_me(personal.load())
        assert "UI 改動不影響功能就直接改（推斷）" in personal.summary(d)

    def test_moving_a_line_up_confirms_it_and_its_evidence_lines_are_not_facts(self):
        personal.about_me_file().write_text(self.MINE)
        personal.save(self._learned())
        text = personal.about_me_file().read_text()
        moved = text.replace("## 不成立\n", "## 習慣\n- UI 改動不影響功能就直接改\n  - 1 次，推斷\n\n## 不成立\n", 1)
        personal.about_me_file().write_text(moved)
        d = personal.import_about_me(personal.load())
        assert "- 習慣：UI 改動不影響功能就直接改" in personal.summary(d)         # confirmed: no （推斷）
        assert not any(f["what"].startswith("1 次") for f in d["facts"])
        personal.save(d)
        assert "UI 改動" not in personal.about_me_file().read_text().split(personal.INFERRED)[1]

    def test_a_不成立_line_takes_an_inferred_fact_out(self):
        personal.about_me_file().write_text(self.MINE)
        personal.save(self._learned())
        personal.about_me_file().write_text(personal.about_me_file().read_text().replace(
            "## 不成立\n", "## 不成立\n- 習慣 UI 改動不影響功能就直接改\n", 1))
        personal.save(personal.import_about_me(personal.load()))
        assert "UI 改動" not in personal.about_me_file().read_text().split(personal.INFERRED)[1]
