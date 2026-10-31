"""Claude Code as an agent meeseek hands work to: one way, no state between
agents. It gets a message and a folder, uses meeseek's plugins and nothing
else of meeseek's, writes to no team system, and waits on no person."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeseek.app import app
from meeseek.config import home
from meeseek.runtime import claude_agent, inbox, messengers


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    yield tmp_path
    # A turn still running in the background would write its record after the
    # paths go back to the real ones: let every one finish first.
    for record_id in list(claude_agent._running):
        claude_agent.wait(record_id, 10)
    claude_agent._running.clear()


class TestPolicy:
    @pytest.fixture
    def root(self, tmp_path):
        (tmp_path / "repo").mkdir()
        return tmp_path / "repo"

    @pytest.mark.parametrize("tool,args", [
        ("Edit", {"file_path": "src/a.py"}),
        ("Write", {"file_path": "{outputs}/pages/report.html"}),
        ("Bash", {"command": "uv run pytest -q"}),           # auto mode decides commands
        ("Read", {"file_path": "/etc/hosts"}),               # and reads
        ("mcp__meeseek__kb_grep", {"pattern": "redis"}),
        ("mcp__meeseek__fetch_url", {"url": "https://example.com"}),
        ("mcp__meeseek__generate_image", {"prompt": "a tavern"}),
        ("mcp__meeseek__review_page", {"path": "pages/report.html"}),
    ])
    def test_auto_mode_may_decide(self, root, tool, args):
        from meeseek.config import home

        args = {k: v.format(outputs=home().outputs) for k, v in args.items()}
        assert claude_agent.policy(root, tool, args) == ""

    @pytest.mark.parametrize("tool,args", [
        ("mcp__meeseek__ask_meeseek", {"request": "open a ticket"}),      # an agent turn
        ("mcp__meeseek__comment_mr", {"iid": 1, "body": "LGTM"}),          # a team-system write
        ("mcp__meeseek__check_meeseek", {"task_id": "x"}),
        ("mcp__other__anything", {}),
        ("Write", {"file_path": "/Users/me/.zshrc"}),
        ("Edit", {"file_path": "../elsewhere/x.py"}),
    ])
    def test_refused_with_a_reason(self, root, tool, args):
        assert claude_agent.policy(root, tool, args)


class TestOptions:
    def _opts(self, tmp_path):
        rec = {"id": "o1", "cwd": str(tmp_path), "handoffs": [], "title": "t", "origin": {}}
        claude_agent.save(rec)
        return rec, claude_agent._options(rec, "", False)

    def test_auto_mode_meeseeks_plugins_and_nothing_else(self, tmp_path):
        _, opts = self._opts(tmp_path)
        assert opts.permission_mode == "auto" and opts.setting_sources == []
        assert set(opts.mcp_servers) == {"meeseek"}
        assert "mcp__meeseek__ask_meeseek" in opts.disallowed_tools
        assert "mcp__meeseek__comment_mr" in opts.disallowed_tools
        assert opts.max_buffer_size >= 32 * 1024 * 1024, "an image Claude reads is one message"
        assert opts.env.get("ENABLE_CLAUDEAI_MCP_SERVERS") == "false", "no claude.ai connectors: no path around meeseek"

    def test_claude_is_told_what_meeseek_knows_about_the_person(self, tmp_path):
        from meeseek.context import personal

        assert "The person you are working for" not in self._opts(tmp_path)[1].system_prompt["append"]  # nothing known yet
        personal.about_me_file().write_text("## 偏好\n- 不要過多註解，命名自己說明意圖\n")
        append = self._opts(tmp_path)[1].system_prompt["append"]
        assert "The person you are working for" in append and "不要過多註解，命名自己說明意圖" in append

    def test_the_hook_refuses_and_records_what_ran(self, tmp_path):
        rec, opts = self._opts(tmp_path)
        hook = opts.hooks["PreToolUse"][0].hooks[0]
        call = lambda tool, args: asyncio.run(hook({"tool_name": tool, "tool_input": args}, None, None))  # noqa: E731
        assert call("Bash", {"command": "uv run pytest -q"}) == {}
        assert call("Read", {"file_path": "a.py"}) == {}
        assert call("mcp__meeseek__ask_meeseek", {})["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert [r["input"] for r in rec["ran"]] == ["Bash uv run pytest -q"]

    def test_when_auto_mode_asks_nobody_is_waited_on(self, tmp_path):
        from meeseek.config import home

        _, opts = self._opts(tmp_path)
        ask = lambda tool, args: asyncio.run(opts.can_use_tool(tool, args, None)).behavior  # noqa: E731
        assert ask("Write", {"file_path": str(home().outputs / "pages" / "x.html")}) == "allow"
        assert ask("mcp__meeseek__review_page", {"path": "pages/x.html"}) == "allow"
        assert ask("Bash", {"command": "rm -rf ~/Desktop/x"}) == "deny"
        assert ask("Read", {"file_path": "/Users/me/.ssh/id_rsa"}) == "deny"


class FakeClient:
    """ClaudeSDKClient, replaying a short session."""

    seen: list = []

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        FakeClient.seen.append((prompt, self.options.resume, self.options.fork_session, self.options.cwd))

    async def interrupt(self):
        pass

    async def receive_response(self):
        import claude_agent_sdk as sdk

        yield sdk.SystemMessage(subtype="init", data={"model": "claude-x", "session_id": "S-new"})
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=3,
                                session_id="S-new", total_cost_usd=0.2, usage={"input_tokens": 10, "output_tokens": 5},
                                result="完成")


@pytest.fixture
def sdk(monkeypatch):
    import claude_agent_sdk

    FakeClient.seen = []
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClient)
    infos = {}
    monkeypatch.setattr(claude_agent, "session_info", lambda sid: infos.get(sid))
    return infos


class TestTurns:
    def test_a_task_is_recorded_with_its_answer(self, sdk, tmp_path):
        rec = claude_agent.wait(claude_agent.start("把 README 的錯字修掉", cwd=str(tmp_path))["id"], 10)
        assert rec["status"] == "done" and rec["session_id"] == "S-new" and rec["title"] == "把 README 的錯字修掉"
        to, back = rec["handoffs"]
        assert (to["from"], to["text"]) == ("meeseek", "把 README 的錯字修掉")
        assert (back["from"], back["text"], back["turns"]) == ("claude", "完成", 3)
        assert "cost_usd" not in back, "no money for the subscription"

    def test_no_folder_is_a_scratch_folder(self, sdk):
        rec = claude_agent.wait(claude_agent.start("想一個名字")["id"], 10)
        assert rec["cwd"].startswith(str(claude_agent.scratch_dir().resolve()))

    def test_continuing_a_session_resumes_it_in_its_own_folder(self, sdk, tmp_path):
        sdk["S-old"] = {"session_id": "S-old", "cwd": str(tmp_path), "branch": "main", "title": "重構",
                        "updated_at": time.time() - 3600}
        rec = claude_agent.wait(claude_agent.start("繼續", session_id="S-old", cwd="/elsewhere")["id"], 10)
        assert FakeClient.seen[-1][1:] == ("S-old", False, str(tmp_path))
        assert rec["title"] == "重構"

    def test_a_session_just_written_elsewhere_is_forked(self, sdk, tmp_path):
        sdk["S-hot"] = {"session_id": "S-hot", "cwd": str(tmp_path), "branch": "", "title": "t",
                        "updated_at": time.time() - 30}
        rec = claude_agent.wait(claude_agent.start("繼續", session_id="S-hot")["id"], 10)
        assert FakeClient.seen[-1][1:3] == ("S-hot", True)
        assert rec["forked_from"] == "S-hot" and rec["session_id"] == "S-new"

    def test_an_unknown_session_or_folder_is_refused(self, sdk):
        with pytest.raises(ValueError):
            claude_agent.start("x", session_id="nope")
        with pytest.raises(ValueError):
            claude_agent.start("x", cwd="/definitely/not/here")

    def test_a_restart_settles_what_was_running(self, tmp_path):
        claude_agent.save({"id": "r1", "session_id": "S", "cwd": str(tmp_path), "title": "t", "status": "running",
                           "handoffs": [], "created_at": 0, "origin": {}})
        assert claude_agent.recover() == 1
        assert claude_agent.get("r1")["status"] == "failed"

    def test_usage_is_calls_and_tokens_not_money(self, sdk, tmp_path):
        import json

        claude_agent.wait(claude_agent.start("x", cwd=str(tmp_path))["id"], 10)
        [row] = [json.loads(x) for x in claude_agent.usage_log().read_text().splitlines()]
        assert (row["source"], row["output"], row["api_estimate_usd"]) == ("claude", 5, 0.2)


class TestSessions:
    def test_every_session_is_listed_with_what_meeseek_and_desktop_know(self, monkeypatch, tmp_path):
        import claude_agent_sdk

        def info(sid, title, cwd, mins):
            return SimpleNamespace(session_id=sid, custom_title=title, summary="", first_prompt=title, cwd=cwd,
                                   git_branch="main", last_modified=(time.time() - mins * 60) * 1000,
                                   created_at=0, file_size=1, tag=None)
        monkeypatch.setattr(claude_agent_sdk, "list_sessions", lambda **kw: [
            info("A", "修 redis 重試", "/r/api", 5), info("B", "寫週報", "/r/docs", 60)])
        claude_agent.save({"id": "r9", "session_id": "B", "cwd": "/r/docs", "title": "寫週報", "status": "done",
                           "handoffs": [], "created_at": 0, "origin": {}})
        desktop = tmp_path / "desktop" / "acct" / "org"
        desktop.mkdir(parents=True)
        (desktop / "local_x.json").write_text('{"cliSessionId": "A", "title": "Redis 重試（Desktop 標題）", "isArchived": false}')
        (desktop / "local_broken.json").write_text("{half")
        monkeypatch.setattr(claude_agent, "DESKTOP_SESSIONS", tmp_path / "desktop")
        rows = claude_agent.sessions()
        assert [(r["session_id"], r["by_meeseek"], r["status"], r["desktop"]) for r in rows] == [
            ("A", False, "", True), ("B", True, "done", False)]
        assert rows[0]["title"] == "Redis 重試（Desktop 標題）"
        assert [r["session_id"] for r in claude_agent.sessions("redis")] == ["A"]

        from meeseek.tools.claude import claude_sessions
        assert "you in Claude Desktop" in claude_sessions("redis")
        assert "session_id: B" in claude_sessions("週報") and "started by meeseek" in claude_sessions("週報")


class Phone:
    name, label, typing_every = "telegram", "Telegram", 60

    def __init__(self):
        self.sent, self.replies = [], []

    def allowed(self, user):
        return user == "7"

    def configured(self):
        return True

    async def send(self, chat, texts):
        self.sent.append((chat, texts))

    async def send_reply(self, chat, markdown, files):
        self.replies.append((chat, markdown))
        return []

    async def send_files(self, chat, paths):
        pass

    async def ask(self, chat, item, action):
        pass

    async def typing(self, chat):
        pass


class TestPhone:
    def test_the_phone_picks_the_session_and_meeseek_hands_the_task_over(self, sdk, tmp_path, monkeypatch):
        phone = Phone()
        messengers.register(phone)
        started = []
        monkeypatch.setattr(inbox, "start", lambda item_id, message="": started.append(message))
        monkeypatch.setattr(claude_agent, "sessions", lambda q="", limit=10: [
            {"session_id": "S-old-0001", "title": "重構 uploader", "cwd": str(tmp_path), "status": ""}])

        asyncio.run(messengers.on_text(phone, "7", "7", "/claude"))
        assert "1. 重構 uploader" in phone.sent[-1][1][0]
        asyncio.run(messengers.on_text(phone, "7", "7", "/claude 1"))
        assert "重構 uploader" in phone.sent[-1][1][0] and not started

        asyncio.run(messengers.on_text(phone, "7", "7", "把剩下的測試補完"))
        assert "[Claude session: S-old-0001 · 重構 uploader]\n把剩下的測試補完" in started[-1]
        [item] = [i for i in inbox.items() if i["source"] == "telegram"]
        assert item["title"] == "把剩下的測試補完"

        asyncio.run(messengers.on_text(phone, "7", "7", "/claude off"))
        app().answered.clear()
        asyncio.run(messengers.on_text(phone, "7", "7", "那現在 sprint 怎樣"))
        assert started[-1] == "那現在 sprint 怎樣"

    def test_an_answer_that_outlived_the_wait_reaches_the_chat_that_asked(self, sdk, tmp_path):
        phone = Phone()
        messengers.register(phone)
        rec = claude_agent.start("跑測試", cwd=str(tmp_path), origin={"channel": "telegram", "chat": "7"})
        claude_agent.wait(rec["id"], 10)
        for _ in range(100):
            if phone.replies:
                break
            time.sleep(0.05)
        assert phone.replies and phone.replies[-1][0] == "7" and "完成" in phone.replies[-1][1]


class TestTool:
    def test_ask_claude_relays_the_answer_when_it_comes_in_time(self, sdk, tmp_path):
        from meeseek.tools.claude import ask_claude

        out = ask_claude("看一下這個 repo 怎麼跑測試", cwd=str(tmp_path), wait_seconds=10)
        assert out.startswith("[Claude session S-new") and "完成" in out and "3 turns" in out
        assert "US$" not in out

    def test_a_bad_folder_says_so(self, sdk):
        from meeseek.tools.claude import ask_claude

        assert ask_claude("x", cwd="/no/such/place").startswith("Claude did not start")


class TestPlugins:
    def test_meeseeks_plugins_are_stateless_lookups_and_drawing_not_agents(self):
        from meeseek.runtime.mcp_server import plugin_tools

        names = set(plugin_tools())
        assert {"kb_grep", "fetch_url", "generate_image", "render_diagram", "review_page"} <= names
        assert names.isdisjoint({"ask_claude", "check_claude", "list_claude_sessions", "ask_meeseek", "check_meeseek",
                                 "comment_mr", "create_mr", "update_mr"})

    def test_a_running_packs_reads_are_plugins_and_its_writes_are_not(self):
        from meeseek.app import build_app
        from meeseek.runtime import claude_agent
        from meeseek.runtime.mcp_server import plugin_tools

        a = build_app()
        a.mcp_tools = _RunningPack()
        with a.active():
            assert {"jira_get_issue"} <= set(plugin_tools())
            assert "jira_create_issue" not in plugin_tools()
            assert claude_agent.policy(Path("/tmp"), "mcp__meeseek__jira_get_issue", {}) == ""
            assert claude_agent.policy(Path("/tmp"), "mcp__meeseek__jira_create_issue", {})

    def test_review_page_looks_only_under_outputs(self, tmp_path, monkeypatch):
        from meeseek.runtime import page_review

        assert "only at pages under" in page_review.review_page("/etc/hosts")
        page = home().outputs / "pages" / "r.html"
        page.parent.mkdir(parents=True)
        page.write_text("<p>x</p>")
        monkeypatch.setattr(page_review, "review", lambda p, purpose: {
            "page": str(p), "ok": False, "summary": "圖表空白", "model": "g", "shots": {},
            "issues": [{"severity": "high", "view": "phone", "where": "圖表", "problem": "沒畫出來", "fix": "畫出來"}]})
        out = page_review.review_page("pages/r.html", "報表")
        assert out.startswith("圖表空白") and "[high·phone] 圖表: 沒畫出來 — fix: 畫出來" in out
        monkeypatch.setattr(page_review, "review", lambda p, purpose: {
            "page": str(p), "ok": True, "summary": "看起來正常", "issues": [], "model": "g", "shots": {}})
        assert page_review.review_page(str(page)) == "OK — 看起來正常"


class _RunningPack:
    """What McpTools reports when a Jira pack is up."""

    def __init__(self):
        from types import SimpleNamespace

        self.servers = [SimpleNamespace(name="jira", agents=["*"])]

    def effects(self):
        return {"jira_get_issue": "read", "jira_create_issue": "write"}

    def server_of(self, tool):
        return "jira"


def test_scratch_folders_go_a_week_after_their_last_run_unless_running(monkeypatch):
    import json
    import os
    import time

    from meeseek.runtime import claude_agent

    root = claude_agent.scratch_dir()
    idle, recent, busy = root / "idle", root / "recent", root / "busy"
    week_ago = time.time() - 8 * 86400
    for d in (idle, recent, busy):
        d.mkdir(parents=True)
        os.utime(d, (week_ago, week_ago))
    # a folder's own time says little (a run writes deep inside it): its task's record says when it ran
    claude_agent.save({"id": "r1", "cwd": str(recent), "status": "done"})
    claude_agent._path("r2").write_text(json.dumps(        # running for over a week, its record untouched
        {"id": "r2", "cwd": str(busy), "status": "running", "updated_at": week_ago}))
    monkeypatch.setattr(claude_agent, "running", lambda rid: rid == "r2")
    assert claude_agent.prune_scratch() == 1
    assert not idle.exists() and recent.exists() and busy.exists()
