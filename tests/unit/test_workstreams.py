"""Workstreams: one shared context per ticket, MR or branch."""

import pytest

from meeseek.runtime import workstreams as ws_mod

REPO = "grp/app"


@pytest.fixture(autouse=True)
def _work(monkeypatch):
    monkeypatch.setattr(ws_mod, "_ticket_projects", lambda: {"PROJ"})
    monkeypatch.setattr(ws_mod, "_default_repo", lambda: REPO)
    monkeypatch.setattr(ws_mod, "update_later", lambda *a, **k: ws_mod.update(*a, **k))   # no thread in tests


def _summary(monkeypatch, **fields):
    from meeseek.runtime import oneshot

    monkeypatch.setattr(oneshot, "ask", lambda *a, **k: ws_mod._Summary(**fields))


def test_the_work_a_text_names():
    text = f"PROJ-12 的 MR https://git.example/{REPO}/-/merge_requests/7 跟 !9，UTF-8 與 SHA-256 不是票，OTHER-1 也不是"
    assert ws_mod.keys_in(text) == ["PROJ-12", f"mr:{REPO}!7", f"mr:{REPO}!9"]


def test_a_ticket_and_its_mr_are_one_workstream_and_many_keys_are_none():
    ws = ws_mod.resolve(["PROJ-12", f"mr:{REPO}!7"])
    assert ws["key"] == "PROJ-12" and ws["aliases"] == [f"mr:{REPO}!7"]
    assert ws_mod.resolve([f"mr:{REPO}!7"])["id"] == ws["id"]          # the MR alone finds it
    assert ws_mod.resolve(["PROJ-1", "PROJ-2", "PROJ-3"]) is None       # a sprint summary is about many


def test_a_conversation_is_attached_by_its_message_and_carries_the_context(monkeypatch):
    _summary(monkeypatch, goal="Ship the reranker fix", state="MR open, waiting for review",
             next="Answer Heidi's comment", decisions=["Use a 0.4 threshold"], title="Reranker fix")
    ws = ws_mod.on_message("web-1", "幫我看 PROJ-12 的狀態")
    assert ws["sessions"] == ["web-1"]
    assert ws_mod.on_message("web-1", "繼續") ["id"] == ws["id"]           # later messages need not name it
    ws_mod.after_turn("web-1", "幫我看 PROJ-12 的狀態", "MR 還在等 review")
    text = ws_mod.instruction("web-1")
    assert "Reranker fix" in text and "Ship the reranker fix" in text and "Use a 0.4 threshold" in text
    assert "MR 還在等 review" in text                                     # the exchange is an event too
    assert ws_mod.instruction("web-other") == ""


def test_a_failed_summary_keeps_the_event(monkeypatch):
    from meeseek.runtime import oneshot

    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(oneshot, "ask", boom)
    ws = ws_mod.on_message("web-1", "PROJ-12")
    ws_mod.update(ws["id"], "PROJ-12 怎麼了", "還在跑")
    assert ws_mod.get(ws["id"])["events"][-1]["text"].endswith("還在跑")


def test_news_is_an_event_and_a_merge_closes_into_the_knowledge_base():
    from meeseek.config.paths import home

    ws = ws_mod.resolve(["PROJ-12", f"mr:{REPO}!7"])
    assert ws_mod.on_event("jira:PROJ-99", "nobody works on this") is None   # news alone opens nothing
    ws_mod.on_event(f"mr:{REPO}!7", "Heidi: 這裡要加測試", kind="review")
    assert ws_mod.get(ws["id"])["events"][-1]["text"] == "Heidi: 這裡要加測試"
    ws_mod.on_event(f"mr:{REPO}!7", "MR merged", kind="merged")
    assert ws_mod.get(ws["id"])["status"] == "closed"
    assert (home().notes / "workstreams" / f"{ws['id']}.md").exists()
    assert ws_mod.rows() == [] and len(ws_mod.rows(include_closed=True)) == 1
    assert ws_mod.resolve(["PROJ-12"])["id"] != ws["id"]                  # new work on it starts a new one


def test_an_opted_in_event_continues_the_claude_session(monkeypatch):
    from meeseek.config import cfg
    from meeseek.runtime import claude_agent

    started = []
    monkeypatch.setattr(claude_agent, "start", lambda prompt, **k: started.append((prompt, k)) or {"id": "r"})
    ws = ws_mod.resolve(["PROJ-12", f"mr:{REPO}!7"])
    ws_mod.attach_claude(ws, "c-1", "/repo")
    ws_mod.on_event(f"mr:{REPO}!7", "Heidi: 這裡要加測試", kind="review")
    assert started == []                                                    # off by default
    monkeypatch.setattr(cfg.workstreams, "route_to_claude", ["review"])
    ws_mod.on_event(f"mr:{REPO}!7", "Heidi: 還有一個地方", kind="review")
    [(prompt, kw)] = started
    assert kw["session_id"] == "c-1" and kw["cwd"] == "/repo" and "還有一個地方" in prompt


def test_a_claude_code_prompt_finds_its_work_by_key_or_branch_and_is_attached():
    ws = ws_mod.resolve(["PROJ-12"])
    assert ws_mod.lookup("hello") is None
    found = ws_mod.lookup("", branch="feature/PROJ-12-reranker", claude_session="c-9", cwd="/repo")
    assert found["id"] == ws["id"] and found["claude"][0]["session_id"] == "c-9"
    assert "branch:feature/PROJ-12-reranker" in found["aliases"]
    assert ws_mod.lookup("anything", claude_session="c-9")["id"] == ws["id"]   # the session is known now
    assert ws_mod.lookup("PROJ-77") is None                                   # a lookup opens nothing


def test_a_hand_over_continues_the_workstreams_claude_session(monkeypatch):
    from meeseek.runtime import claude_agent
    from meeseek.tools import claude as claude_tool

    ws = ws_mod.on_message("web-1", "PROJ-12")
    ws_mod.attach_claude(ws, "c-1", "/repo")
    monkeypatch.setattr(claude_agent, "record_of", lambda sid: {"id": "r1"} if sid == "c-1" else None)
    monkeypatch.setattr(claude_agent, "running", lambda rid: False)
    prompt, session_id, origin = claude_tool._with_workstream("fix the test", "/repo", "", {"session_id": "web-1"})
    assert session_id == "c-1" and origin["workstream"] == ws["id"]
    assert prompt.startswith("## Workstream") and prompt.endswith("fix the test")
    monkeypatch.setattr(claude_agent, "running", lambda rid: True)          # busy: a new session, still with context
    _, session_id, _ = claude_tool._with_workstream("another", "/repo", "", {"session_id": "web-1"})
    assert session_id == ""


def test_a_claude_answer_folds_into_the_workstream(monkeypatch):
    import asyncio

    _summary(monkeypatch, state="Test fixed on the branch")
    ws = ws_mod.resolve(["PROJ-12"])
    record = {"session_id": "c-1", "cwd": "/repo", "origin": {"workstream": ws["id"]},
              "handoffs": [{"from": "meeseek", "text": "## Workstream …\n\n---\n\nfix the test"},
                           {"from": "claude", "text": "Fixed; 12 tests pass."}]}
    asyncio.run(ws_mod.on_claude_turn(record))
    got = ws_mod.get(ws["id"])
    assert got["claude"][0]["session_id"] == "c-1" and got["state"] == "Test fixed on the branch"
    assert got["events"][-1]["text"] == "fix the test → Fixed; 12 tests pass."


def test_the_plugins_read_and_note():
    ws_mod.workstream_note("PROJ-12", "Decided: keep the old API until v2", _origin={"user": "claude"})
    text = ws_mod.workstream_get("PROJ-12")
    assert "Decided: keep the old API until v2" in text
    assert "No workstream" in ws_mod.workstream_get("PROJ-404")


def test_the_console_lists_them_and_the_hook_looks_one_up():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from meeseek.surfaces import console

    ws_mod.resolve(["PROJ-12"])
    app = FastAPI()
    console.install(app)
    client = TestClient(app)
    assert [r["key"] for r in client.get("/api/workstreams").json()] == ["PROJ-12"]
    hit = client.get("/api/workstreams/lookup", params={"branch": "feat/PROJ-12-x", "claude_session": "c-2"}).json()
    assert hit["key"] == "PROJ-12" and hit["context"].startswith("## Workstream")
    assert client.get("/api/workstreams/lookup", params={"text": "nothing here"}).json() == {}
