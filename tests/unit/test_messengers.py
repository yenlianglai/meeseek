"""Phone messengers: a chat message is an Inbox item, its answer goes back to
the chat, and a held write is approved from there."""

import asyncio
import json

import pytest

from meeseek.app import app
from meeseek.runtime import inbox, messengers
from meeseek.runtime.messengers import telegram
from meeseek.config import home

_REAL_SLEEP = asyncio.sleep   # before any test swaps it out


class FakeChannel:
    name = "telegram"
    label = "Telegram"
    typing_every = 60

    def __init__(self, allowed=("u1",)):
        self._allowed = set(allowed)
        self.sent: list[tuple[str, list[str]]] = []
        self.replies: list[tuple[str, str]] = []
        self.files: list[list[str]] = []
        self.asked: list[tuple[str, str]] = []

    def allowed(self, user):
        return user in self._allowed

    def configured(self):
        return bool(self._allowed)

    async def send(self, chat, texts):
        self.sent.append((chat, texts))

    async def send_reply(self, chat, markdown, files):
        self.replies.append((chat, markdown))
        return []

    async def send_files(self, chat, paths):
        self.files.append([p.name for p in paths])

    async def ask(self, chat, item, action):
        self.asked.append((chat, action["id"]))

    async def typing(self, chat):
        pass


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    turn_texts: list[str] = []

    async def fake_turn_texts(item):
        return list(turn_texts)
    monkeypatch.setattr(messengers, "_turn_texts", fake_turn_texts)
    monkeypatch.setattr(messengers, "TURN_TEXTS", turn_texts, raising=False)
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(inbox, "start", lambda item_id, message="": started.append((item_id, message)))
    return started


def run(coro):
    async def go():
        out = await coro
        await asyncio.sleep(0)   # let the typing task see the loop once
        return out
    return asyncio.run(go())


def _items():
    return [i for i in inbox.items() if i["source"] == "telegram"]


class TestText:
    def test_markdown_becomes_chat_text(self):
        md = "## 結論\n**3 個** MR 待審，見 [ACME-1](https://jira/x) 與 `main`。\n```bash\nls\n```"
        assert messengers.plain(md) == "【結論】\n3 個 MR 待審，見 ACME-1 https://jira/x 與 main。\nls"

    def test_long_answers_split_at_paragraphs(self):
        text = "\n\n".join(["a" * 30] * 10)
        parts = messengers.chunks(text, size=100)
        assert all(len(p) <= 100 for p in parts)
        assert "".join(parts).replace("\n", "") == text.replace("\n", "")

    def test_a_code_block_is_never_cut_open(self):
        text = "前言\n\n```python\n" + "\n".join(f"x{i} = {i}" for i in range(40)) + "\n```\n\n結尾"
        parts = messengers.chunks(text, size=120)
        assert all(len(p) <= 120 for p in parts)
        for p in parts:
            assert p.count("```") % 2 == 0, p   # every message opens what it closes

    def test_markdown_renders_as_telegram_html(self):
        md = ("## 結論\n**3 個** MR 待審 <急>，見 [ACME-1](https://jira/x?a=1&b=2) 與 `a<b`。\n"
              "- 第一\n  - 子項\n> 引用\n![圖](/outputs/images/x.png)\n```bash\necho <hi>\n```")
        assert telegram.to_html(md) == (
            "<b>結論</b>\n<b>3 個</b> MR 待審 &lt;急&gt;，見 <a href=\"https://jira/x?a=1&amp;b=2\">ACME-1</a> 與 <code>a&lt;b</code>。\n"
            "• 第一\n  • 子項\n<blockquote>引用</blockquote>\n\n"
            '<pre><code class="language-bash">echo &lt;hi&gt;</code></pre>')

    def test_a_table_becomes_aligned_columns(self):
        out = telegram.to_html("| 環境 | 狀態 |\n|---|---|\n| prod | ok |\n| uat 環境 | down |")
        assert out == "<pre>環境      狀態\nprod      ok\nuat 環境  down</pre>"


class TestConversation:
    def test_an_unknown_sender_is_told_their_id_until_ids_are_listed(self, isolated):
        ch = FakeChannel(allowed=())
        run(messengers.on_text(ch, "c9", "u9", "hi"))
        assert "u9" in ch.sent[0][1][0] and not isolated

        ch = FakeChannel(allowed=("u1",))
        run(messengers.on_text(ch, "c9", "u9", "hi"))
        assert ch.sent == [] and not isolated and _items() == []

    def test_a_message_starts_a_conversation_and_the_next_continues_it(self, isolated):
        ch = FakeChannel()
        run(messengers.on_text(ch, "c1", "u1", "ACME-1 現在什麼狀態？"))
        [item] = _items()
        assert item["meta"]["chat"] == "c1" and item["title"] == "ACME-1 現在什麼狀態？"
        assert "Telegram" in isolated[0][1] and "ACME-1 現在什麼狀態？" in isolated[0][1]

        app().answered.clear()
        run(messengers.on_text(ch, "c1", "u1", "那 ACME-2 呢"))
        assert len(_items()) == 1 and isolated[1] == (item["id"], "那 ACME-2 呢")

    def test_a_message_during_a_turn_waits_for_it(self, isolated):
        ch = FakeChannel()
        messengers.channels()["telegram"] = ch
        run(messengers.on_text(ch, "c1", "u1", "第一則"))
        run(messengers.on_text(ch, "c1", "u1", "第二則"))
        [item] = _items()
        assert item["meta"]["queued"] == ["第二則"] and len(isolated) == 1

        item["reply"], item["status"] = "好", "ready"
        inbox.save(item)
        run(messengers.deliver(item))
        assert isolated[-1] == (item["id"], "第二則")
        assert "queued" not in inbox.get(item["id"])["meta"]

    def test_new_starts_over(self, isolated):
        ch = FakeChannel()
        run(messengers.on_text(ch, "c1", "u1", "第一則"))
        app().answered.clear()
        run(messengers.on_text(ch, "c1", "u1", "/new"))
        run(messengers.on_text(ch, "c1", "u1", "另一件事"))
        assert len(_items()) == 2


class TestDelivery:
    def _item(self, **kw):
        item = inbox.file(source="telegram", key=kw.pop("key", "telegram:c1:x"), title="t", meta={"chat": "c1"})
        item.update(kw)
        inbox.save(item)
        return item

    def test_the_answer_goes_back_and_a_held_write_is_asked_once(self):
        ch = FakeChannel()
        messengers.channels()["telegram"] = ch
        item = self._item(reply="**好**，我開單。", status="needs_approval", actions=[
            {"id": "a1", "tool": "create_issue", "args": {"summary": "x"}, "status": "pending"}])
        run(messengers.deliver(item))
        assert ch.replies == [("c1", "**好**，我開單。")] and ch.asked == [("c1", "a1")]
        run(messengers.deliver(inbox.get(item["id"])))
        assert len(ch.asked) == 1

    def test_an_answered_item_is_done_and_a_failed_one_says_so(self):
        ch = FakeChannel()
        messengers.channels()["telegram"] = ch
        item = self._item(reply="完成", status="ready")
        run(messengers.deliver(item))
        assert inbox.get(item["id"])["status"] == "done"

        item = self._item(key="telegram:c1:y", status="failed", error="Timeout")
        run(messengers.deliver(item))
        assert "Timeout" in ch.sent[-1][1][0]

    def test_items_from_elsewhere_are_not_delivered(self):
        ch = FakeChannel()
        messengers.channels()["telegram"] = ch
        run(messengers.deliver(inbox.file(source="gmail", key="g", title="t")))
        assert ch.sent == [] and ch.replies == []

    def test_files_the_turn_made_follow_the_answer_once(self, tmp_path):
        ch = FakeChannel()
        messengers.channels()["telegram"] = ch
        (home().outputs / "images").mkdir(parents=True, exist_ok=True)
        (home().outputs / "images" / "cat.png").write_bytes(b"png")
        (home().outputs / "reports").mkdir()
        (home().outputs / "reports" / "r.md").write_text("# r")
        messengers.TURN_TEXTS.append('{"result": "Generated → knowledge_base/outputs/images/cat.png"}')
        item = self._item(reply="報告在 outputs/reports/r.md，圖：![x](/outputs/images/cat.png)", status="ready")
        run(messengers.deliver(item))
        assert ch.files == [["r.md", "cat.png"]]
        run(messengers.deliver(inbox.get(item["id"])))
        assert len(ch.files) == 1

    def test_attached_paths_leave_the_text(self, tmp_path):
        (home().outputs / "images").mkdir(parents=True, exist_ok=True)
        img = home().outputs / "images" / "cat.png"
        img.write_bytes(b"png")
        doc = home().outputs / "r.md"
        doc.write_text("r")
        md = "畫好了。\n\n圖片：outputs/images/cat.png\n![cat](/outputs/images/cat.png)\n報告見 outputs/r.md"
        assert messengers.detach(md, [img, doc]) == "畫好了。\n\n圖片：（見下方圖片）\n（見下方圖片）\n報告見 （附檔：r.md）"
        assert messengers.detach(md, [img], caption=True) == "畫好了。\n\n報告見 outputs/r.md"

    def test_only_existing_files_inside_outputs_are_sent(self, tmp_path):
        (tmp_path / "secret.md").write_text("no")
        (home().outputs / "ok.md").write_text("yes")
        found = messengers.output_files("outputs/../secret.md outputs/missing.md outputs/ok.md")
        assert [p.name for p in found] == ["ok.md"]


class TestChoice:
    def _held(self):
        item = inbox.file(source="telegram", key="telegram:c1:z", title="t", meta={"chat": "c1"})
        item["actions"] = [{"id": "a1", "tool": "create_issue", "args": {}, "status": "pending"}]
        item["session_id"] = "inbox-x"
        inbox.save(item)
        return item

    def test_approve_runs_the_next_turn(self, isolated):
        item = self._held()
        said = run(messengers.on_choice(FakeChannel(), "c1", "u1", f"a:{item['id']}:a1"))
        assert said.startswith("已同意")
        assert inbox.get(item["id"])["actions"][0]["status"] == "approved"
        assert isolated[0][0] == item["id"] and "create_issue" in isolated[0][1]

    def test_a_second_tap_or_another_chat_changes_nothing(self, isolated):
        item = self._held()
        ch = FakeChannel()
        assert run(messengers.on_choice(ch, "c2", "u1", f"a:{item['id']}:a1")) == "找不到這個動作。"
        run(messengers.on_choice(ch, "c1", "u1", f"r:{item['id']}:a1"))
        assert run(messengers.on_choice(ch, "c1", "u1", f"a:{item['id']}:a1")) == "這個動作已經處理過了。"
        assert inbox.get(item["id"])["actions"][0]["status"] == "rejected"


class TestTelegram:
    def test_a_tap_settles_the_action_and_takes_the_buttons_off(self, isolated, monkeypatch):
        ch = telegram.Telegram("t", ["7"])
        calls = []

        async def fake_call(method, **params):
            calls.append(method)
            return {}
        monkeypatch.setattr(ch, "call", fake_call)
        item = inbox.file(source="telegram", key="telegram:7:z", title="t", meta={"chat": "7"})
        item["actions"] = [{"id": "a1", "tool": "create_issue", "args": {}, "status": "pending"}]
        inbox.save(item)
        run(ch.handle({"update_id": 1, "callback_query": {
            "id": "q", "from": {"id": 7}, "data": f"a:{item['id']}:a1",
            "message": {"message_id": 5, "chat": {"id": 7}}}}))
        assert [c for c in calls if c != "sendChatAction"] == ["answerCallbackQuery", "editMessageReplyMarkup", "sendMessage"]
        assert isolated and isolated[0][0] == item["id"]

    def test_one_picture_and_a_short_answer_are_one_message(self, tmp_path, monkeypatch):
        img = home().outputs / "cat.png"
        img.write_bytes(b"png")
        ch = telegram.Telegram("t", ["7"])
        sent = []

        async def fake_upload(method, data, files):
            sent.append((method, data.get("caption"), list(files)))
        async def fake_call(method, **params):
            sent.append((method, params.get("text"), []))
        monkeypatch.setattr(ch, "upload", fake_upload)
        monkeypatch.setattr(ch, "call", fake_call)
        done = run(ch.send_reply("7", "**畫好了**\n\n圖片：outputs/cat.png", [img]))
        assert done == [img] and sent == [("sendPhoto", "<b>畫好了</b>", ["photo"])]

        sent.clear()
        done = run(ch.send_reply("7", "長" * 1200 + "\n\noutputs/cat.png", [img]))
        assert done == [] and sent[0][0] == "sendMessage" and "（見下方圖片）" in sent[-1][1]

    def test_a_page_goes_as_a_picture_of_itself_and_the_file(self, tmp_path, monkeypatch):
        page = tmp_path / "report.html"
        page.write_text("<p>hi</p>")
        shot = tmp_path / "shot.png"
        shot.write_bytes(b"png")
        monkeypatch.setattr(telegram.page_shot, "screenshot", lambda p: shot)
        ch = telegram.Telegram("t", ["7"])
        sent = []

        async def fake_upload(method, data, files):
            sent.append((method, data.get("caption"), list(files)))
        monkeypatch.setattr(ch, "upload", fake_upload)
        run(ch.send_files("7", [page]))
        assert sent == [("sendPhoto", "report.html", ["photo"]), ("sendDocument", None, ["document"])]

    def test_the_offset_survives_a_restart(self):
        telegram._save_offset(42)
        assert telegram._load_offset() == 42
        assert json.loads(telegram.state_file().read_text()) == {"offset": 42}


class TestTelegramNetwork:
    """The laptop may be on a hotspot that comes and goes: a send rides it out,
    a refusal does not, and the poll starts even if the network is not up yet."""

    @pytest.fixture
    def net(self, monkeypatch):
        import httpx

        script: list = []      # what each request gets: a response or an exception
        hits: list[str] = []   # the API method of each request
        waits: list[float] = []
        real = httpx.AsyncClient

        def handler(request):
            hits.append(request.url.path.rsplit("/", 1)[-1])
            if not script:   # past the script: the network is down again
                raise httpx.ConnectError("script over")
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        monkeypatch.setattr(telegram.httpx, "AsyncClient",
                            lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

        # Waits take no real time, but the module's clock moves by them, so a
        # retry window runs out as it would. Only the module's view of time is
        # swapped: asyncio's own loop clock stays real.
        clock = [0.0]

        async def no_wait(s):
            if s:   # run()'s own sleep(0) is not a wait under test
                waits.append(s)
                clock[0] += s
            await _REAL_SLEEP(0)   # still yield, or a retrying loop never lets go
        monkeypatch.setattr(telegram.asyncio, "sleep", no_wait)
        monkeypatch.setattr(telegram, "time", type("Clock", (), {"monotonic": staticmethod(lambda: clock[0])}))

        def ok(result=None):
            return httpx.Response(200, json={"ok": True, "result": result})

        def err(status, description="", **parameters):
            return httpx.Response(status, json={"ok": False, "description": description, "parameters": parameters})

        return {"script": script, "hits": hits, "waits": waits, "ok": ok, "err": err, "httpx": httpx}

    def test_a_reply_rides_out_a_dropped_network_and_a_5xx(self, net):
        net["script"] += [net["httpx"].ConnectError("no route"), net["err"](502, "Bad Gateway"), net["ok"]({"message_id": 1})]
        ch = telegram.Telegram("t", ["7"])
        assert run(ch.call("sendMessage", chat_id="7", text="hi")) == {"message_id": 1}
        assert net["hits"] == ["sendMessage"] * 3
        assert net["waits"] == [2.0, 4.0]

    def test_a_429_waits_as_long_as_telegram_says(self, net):
        net["script"] += [net["err"](429, "Too Many Requests", retry_after=7), net["ok"](True)]
        run(telegram.Telegram("t", ["7"]).call("sendMessage", chat_id="7", text="hi"))
        assert net["waits"] == [7.0]

    def test_a_refusal_is_not_retried(self, net):
        """A 400 (HTML Telegram cannot parse) must fail at once: the caller's
        plain-text fallback depends on it."""
        net["script"] += [net["err"](400, "can't parse entities")]
        with pytest.raises(RuntimeError, match="can't parse"):
            run(telegram.Telegram("t", ["7"]).call("sendMessage", chat_id="7", text="<b"))
        assert net["hits"] == ["sendMessage"] and net["waits"] == []

    def test_a_send_gives_up_after_the_retry_window(self, net, monkeypatch):
        monkeypatch.setattr(telegram, "_RETRY_S", 5)   # 2 s, then 4 s would pass the window
        net["script"] += [net["httpx"].ConnectError("down")] * 3
        with pytest.raises(net["httpx"].ConnectError):
            run(telegram.Telegram("t", ["7"]).call("sendMessage", chat_id="7", text="hi"))
        assert net["waits"] == [2.0]

    def test_without_retry_a_failure_raises_at_once(self, net):
        net["script"] += [net["httpx"].ConnectError("down")]
        with pytest.raises(net["httpx"].ConnectError):
            run(telegram.Telegram("t", ["7"]).call("sendChatAction", retry=False, chat_id="7", action="typing"))
        assert net["waits"] == []

    def test_the_poll_starts_even_when_the_network_is_not_up_yet(self, net, isolated):
        """deleteWebhook failing at startup used to end the poll task for good."""
        net["script"] += [
            net["httpx"].ConnectError("wifi not up"),   # deleteWebhook, first try
            net["ok"](True),                            # deleteWebhook, again
            net["ok"]([]),                              # a first, empty poll
        ]
        ch = telegram.Telegram("t", ["7"])

        async def poll_three_requests():
            task = asyncio.ensure_future(ch.run())
            for _ in range(1000):
                if len(net["hits"]) >= 3 or task.done():
                    break
                await _REAL_SLEEP(0)
            assert not task.done(), task.exception()   # still polling, not dead
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run(poll_three_requests())
        assert net["hits"][:3] == ["deleteWebhook", "deleteWebhook", "getUpdates"]
        assert net["waits"][0] == 5   # the poll's own backoff, not a send's retry


def test_a_coached_meeting_without_a_report_says_why_once(monkeypatch, tmp_path):
    """2026-10-21: the daily was never recorded and nobody heard."""
    import asyncio

    from meeseek.config import cfg
    from meeseek.runtime import inbox, messengers

    monkeypatch.setattr(cfg.triggers, "coach_telegram_chat", "42")
    monkeypatch.setattr("meeseek.runtime.meeting_coach.coached", lambda title: title == "Data team daily")
    sent = []

    class Ch:
        async def send(self, chat, texts):
            sent.append((chat, texts))

    monkeypatch.setitem(messengers.channels(), "telegram", Ch())
    item = inbox.file(source="meeting", key="meeting:x", title="Recording: Data team daily", meta={"code": "abc"})
    item["status"], item["error"] = "failed", "Meet's recording is not shared with you"
    inbox.save(item)
    asyncio.run(messengers.meeting_failed(item))
    asyncio.run(messengers.meeting_failed(inbox.get(item["id"])))
    assert sent == [("42", ["會後報告沒有產生 · Data team daily\nMeet's recording is not shared with you"])]


def test_a_thumb_on_the_phone_is_recorded_and_a_thumbs_down_asks_why(monkeypatch, tmp_path):
    import asyncio

    from meeseek.runtime import feedback, inbox, messengers

    monkeypatch.setattr(inbox, "last_invocation", lambda session: "inv-9")
    item = inbox.file(source="telegram", key="tg:1", title="t", meta={"chat": "42"})
    item["session_id"] = "inbox-x"
    inbox.save(item)
    sent = []

    class Ch:
        name = "telegram"

        def allowed(self, user):
            return True

        async def send(self, chat, texts):
            sent.extend(texts)

    ch = Ch()
    assert asyncio.run(messengers.on_choice(ch, "42", "u", f"f:+:{item['id']}")) == "收到 👍"
    assert asyncio.run(messengers.on_choice(ch, "42", "u", f"f:-:{item['id']}")) == messengers.ASK_WHY
    asyncio.run(messengers.on_note(ch, "42", "u", "漏了 MR 的連結"))
    thumbs = [(o["value"], o.get("note", "")) for o in feedback.read() if o["kind"] == "thumb"]
    assert thumbs == [(1, ""), (-1, ""), (-1, "漏了 MR 的連結")] and sent       # the note is not a new turn
