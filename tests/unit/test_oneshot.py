"""One-shot model calls go through the configured model, as agent turns do
(runtime/oneshot.py) — so none of them needs Gemini. Each converted call is
driven here by the scripted model."""

import asyncio
import json

import pytest

from meeseek.adapters.scripted import Scripted, fail, say
from meeseek.core.turn import TransientModelError
from meeseek.runtime import oneshot


@pytest.fixture
def model(monkeypatch):
    """The scripted model every one-shot call gets; `.script(...)` sets what it answers."""
    built = []

    class Models:
        def script(self, *steps):
            self.m = Scripted(*steps)
            return self.m

    models = Models()
    models.script()

    def build(name, by=""):
        built.append((name, by))
        return models.m

    monkeypatch.setattr(oneshot, "build_model", build)
    models.built = built
    return models


# --- the helper ---------------------------------------------------------------------

def test_text_in_text_out_through_the_configured_model(model):
    m = model.script(say("  好的  "))
    assert oneshot.ask("說好", model="any-model", system="sys", temperature=0.3) == "好的"
    assert model.built == [("any-model", "test_oneshot")], "built by name; the caller's module is the source"
    assert m.seen[0].system == "sys" and m.seen[0].messages[0].text == "說好"
    assert m.seen[0].options == {"temperature": 0.3, "timeout_s": 300}


def test_a_schema_answer_is_parsed_even_in_a_code_fence(model):
    from pydantic import BaseModel

    class Answer(BaseModel):
        n: int

    m = model.script(say('```json\n{"n": 3}\n```'))
    assert oneshot.ask("?", model="x", schema=Answer).n == 3
    assert m.seen[0].options["schema"] is Answer
    model.script(say(""))
    with pytest.raises(ValueError, match="answered nothing"):
        oneshot.ask("?", model="x", schema=Answer)


def test_files_lead_the_prompt_in_order_and_are_never_stored(model):
    from meeseek.adapters import llm
    from meeseek.adapters.blobs import blobs

    m = model.script(say("ok"))
    oneshot.ask("看圖", model="x", files=[("image/png", b"P1"), ("audio/mp4", b"A2")])
    assert m.seen[0].files == [("image/png", b"P1"), ("audio/mp4", b"A2")]
    assert llm._INLINE == {}, "released after the call"
    assert not blobs().root.exists() or not any(p.is_file() for p in blobs().root.rglob("*")), "nothing stored"


def test_usage_is_recorded_under_the_source(model, _home):
    from meeseek.core.events import ModelStepped, Usage
    from meeseek.runtime.usage import direct_log

    class Counted:
        async def step(self, **kw):
            return ModelStepped(session="s", turn="t", by="b", text="x",
                                usage=Usage(model="m-1", input_tokens=10, output_tokens=4, cached_tokens=2))

    model.m = Counted()
    oneshot.ask("?", model="x", source="rl_judge")
    [line] = [json.loads(ln) for ln in direct_log().read_text().splitlines()]
    assert (line["source"], line["model"], line["prompt"], line["output"], line["cached"]) == ("rl_judge", "m-1", 10, 4, 2)


def test_a_transient_failure_is_asked_again_once(model, monkeypatch):
    async def no_wait(s):
        pass

    monkeypatch.setattr(oneshot.asyncio, "sleep", no_wait)
    model.script(fail(TransientModelError("429")), say("ok"))
    assert oneshot.ask("?", model="x") == "ok"
    model.script(fail(TransientModelError("429")), fail(TransientModelError("429")))
    with pytest.raises(TransientModelError):
        oneshot.ask("?", model="x")


def test_from_inside_a_running_loop_it_gets_a_thread(model):
    model.script(say("ok"))

    async def inside():
        return oneshot.ask("?", model="x")

    assert asyncio.run(inside()) == "ok"


# --- each converted call ------------------------------------------------------------

def test_triage(model):
    from meeseek.config import cfg
    from meeseek.runtime import triggers

    m = model.script(say('{"action": "reply", "summary": "要回", "why": "對方在等"}'))
    assert triggers.triage("gmail", "a@x", "請看", "內容") == {"action": "reply", "summary": "要回", "why": "對方在等"}
    assert model.built[0][0] == cfg.models.fast and m.seen[0].options["schema"] is triggers.Triage
    model.script(fail(RuntimeError("down")))
    assert triggers.triage("gmail", "a@x", "請看", "內容")["action"] == "fyi"


def test_page_review_sends_both_screenshots(model, monkeypatch, tmp_path):
    from meeseek.runtime import page_review

    shots = {500: tmp_path / "p.png", 1280: tmp_path / "d.png"}
    shots[500].write_bytes(b"PHONE")
    shots[1280].write_bytes(b"DESK")
    monkeypatch.setattr(page_review.page_shot, "screenshot", lambda page, w, h: shots[w])
    m = model.script(say('{"ok": true, "summary": "看起來好", "issues": []}'))
    r = page_review.review(tmp_path / "x.html", "報告")
    assert r["ok"] and r["summary"] == "看起來好"
    assert m.seen[0].files == [("image/png", b"PHONE"), ("image/png", b"DESK")]
    assert "先是手機" in m.seen[0].messages[0].text, "the prompt names the screenshots in the order they come"


def test_the_turn_judge(model):
    from meeseek.runtime import reward

    m = model.script(say('{"score": 4, "tags": ["made_up_tag"], "why": "好"}'))
    out = reward.judge({"skill": "code", "request": "看 MR", "calls": [("get_mr", False)], "reply_head": "LGTM"})
    assert out == {"score": 4, "tags": [], "why": "好"} and m.seen[0].options["max_output_tokens"] == 600
    assert model.built[0][1] == "rl_judge"


def test_the_rule_judge(model):
    from meeseek.runtime import rule_review

    model.script(say('{"follows": true, "why": "有附連結"}'))
    assert rule_review.judge("附連結", "問", "答 http://x", []) == {"follows": True, "why": "有附連結"}
    model.script(fail(RuntimeError("down")))
    assert rule_review.judge("附連結", "問", "答", [])["follows"] is None


def test_the_improvers_calls_are_booked_then_made(model):
    from pydantic import BaseModel

    from meeseek.runtime import improve

    class Out(BaseModel):
        summary: str

    m = model.script(say('{"summary": "改這裡"}'))
    got = improve._ask("提案", Out, model="x", budget=improve.Budget(usd=1.0), step="propose", usd=0.01)
    assert got.summary == "改這裡" and m.seen[0].options["temperature"] == 0.2
    assert improve._ask("提案", Out, model="x", budget=improve.Budget(usd=0.0), step="propose", usd=0.01) is None


def test_the_team_graph_learns_from_a_meeting(model):
    from meeseek.context import ontology

    rel, (subj_classes, obj_classes) = next(iter(ontology.RELATIONS.items()))
    triple = {"subject": "Ann", "subject_class": subj_classes[0], "relation": rel, "object": "Core",
              "object_class": obj_classes[0], "quote": "Ann 在 Core"}
    model.script(say(json.dumps({"triples": [triple]}, ensure_ascii=False)))
    assert ontology.learn("逐字稿", "筆記", title="週會", date="2026-10-24") == 1


def test_what_holds_of_you_is_learned_from_chat(model, monkeypatch):
    from meeseek.context import personal

    monkeypatch.setattr(personal, "_conversations", lambda since: [("2026-10-24 web-1", "我：我比較喜歡先看結論")])
    model.script(say(json.dumps({"facts": [{"conversation": 1, "rel": "偏好", "what": "先講結論",
                                            "quote": "我比較喜歡先看結論"}]}, ensure_ascii=False)))
    assert personal.learn_from_chat(days=1) == 1


def test_meeting_notes_from_a_transcript(model):
    from meeseek.context import meeting_notes

    model.script(say("# 週會\n- 決定：先修登入"))
    path = meeting_notes.notes_from_transcript("00:00 我：先修登入", "週會.m4a", date="2026-10-24")
    assert path.read_text(encoding="utf-8").startswith("# 週會")


def test_the_coach_hears_the_mic_when_it_is_short(model, monkeypatch, tmp_path):
    from meeseek.runtime import meeting_coach
    from meeseek.runtime.meeting_transcript import Line

    (tmp_path / "mic.m4a").write_bytes(b"MIC")
    monkeypatch.setattr(meeting_coach, "duration", lambda p: 60.0)

    def cut(src, start, length, dst):
        dst.write_bytes(b"SMALL")
        return dst

    monkeypatch.setattr(meeting_coach, "cut", cut)
    m = model.script(say("## 表現\n說得清楚"))
    files = meeting_coach.report(tmp_path, [Line("00:00", "我", "大家好")], title="週會", started=1_000_000.0)
    assert "report" in files and m.seen[0].files == [("audio/mp4", b"SMALL")]
    assert "附上的音訊" in m.seen[0].messages[0].text


def test_a_transcript_window_that_comes_back_empty_is_a_gap_not_a_failure(model, monkeypatch, tmp_path):
    """2026-10-21: one window came back empty and the whole two-hour meeting failed."""
    from meeseek.runtime import meeting_transcript as mt

    (tmp_path / "mic.m4a").write_bytes(b"M")
    (tmp_path / "app.m4a").write_bytes(b"A")
    monkeypatch.setattr(mt, "duration", lambda p: 700.0)          # two windows: 0 and 600

    def cut(src, start, length, dst):
        dst.write_bytes(src.read_bytes() + str(start).encode())
        return dst

    monkeypatch.setattr(mt, "cut", cut)
    m = model.script(say('{"turns": [{"at": "00:05", "who": "我", "text": "大家好"}]}'), say(""), say("not json"))
    lines = mt.transcript(tmp_path)
    assert [(ln.at, ln.who) for ln in lines] == [("00:05", "我"), ("10:00", mt.GAP)]
    assert mt.said(lines) == 3, "a gap is not speech"
    first = m.seen[0]
    assert first.files == [("audio/mp4", b"M0"), ("audio/mp4", b"A0")]
    assert "音訊 1" in first.messages[0].text and first.options["thinking"] == "LOW"
    assert first.options["timeout_s"] == 900 and first.options["schema"] is mt._Turns
