"""The goldfish review: zero-context reviewers, their merged findings, the
ask_claude gate (blockers back once, then the brief goes) and review_spec."""

import pytest

from meeseek.runtime import claude_agent, goldfish, oneshot, packs
from meeseek.runtime import workstreams as ws_mod
from meeseek.tools import claude as claude_tools

BLOCKER = {"severity": "blocker", "where": "Plan", "what": "The plan does not say which module owns the cache.",
           "suggestion": "Name the module and its path."}
MAJOR = {"severity": "major", "where": "Scope", "what": "A plugin system is built that the goal does not need.",
         "suggestion": "Drop it."}


def _lens(prompt: str) -> str:
    return next(lens for lens, mark in (("comprehension", "Your lens: comprehension"),
                                         ("zoom_out", "Your lens: zoom out"),
                                         ("readiness", "Your lens: implementation readiness"))
                if mark in prompt)


@pytest.fixture
def reviewers(monkeypatch):
    """oneshot.aask as three scripted reviewers: `answers[lens]` is a list of
    findings, or an exception the reviewer raises. Every prompt is kept."""
    seen: list[str] = []
    answers: dict = {"comprehension": [], "zoom_out": [], "readiness": []}

    async def aask(prompt, **kw):
        seen.append(prompt)
        got = answers[_lens(prompt)]
        if isinstance(got, Exception):
            raise got
        return goldfish.Findings(findings=[goldfish.Finding(**f) for f in got])

    monkeypatch.setattr(oneshot, "aask", aask)
    return answers, seen


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(claude_agent, "start", lambda prompt, **k: calls.append((prompt, k)) or {"id": "r1"})
    monkeypatch.setattr(claude_agent, "wait", lambda rid, t: {"id": rid, "cwd": "/repo", "session_id": "c-1",
                                                              "handoffs": [{"from": "claude", "text": "done"}]})
    monkeypatch.setattr(claude_agent, "running", lambda rid: False)
    return calls


def test_two_reviewers_saying_the_same_thing_is_one_finding_at_the_higher_severity():
    a = [{**BLOCKER, "lenses": ["readiness"]}]
    b = [{**BLOCKER, "severity": "major", "what": "The plan does not say which module owns the cache!",
          "suggestion": "Name the module, its path and its owner.", "lenses": ["comprehension"]},
         {**MAJOR, "severity": "nit", "lenses": ["comprehension"]}]
    merged = goldfish.merge([b, a])
    assert [f["severity"] for f in merged] == ["blocker", "nit"]                   # blockers first
    assert merged[0]["lenses"] == ["comprehension", "readiness"]
    assert merged[0]["suggestion"] == "Name the module, its path and its owner."    # the fuller suggestion


def test_the_reviewers_see_the_document_and_its_references_only(reviewers, monkeypatch, tmp_path, _home):
    answers, seen = reviewers
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cache.py").write_text("def get(key): ...\n", encoding="utf-8")
    (_home.sources / "drive").mkdir(parents=True, exist_ok=True)
    (_home.sources / "drive" / "design.md").write_text("# Cache design\nTTL is 60 s.\n", encoding="utf-8")
    monkeypatch.setattr("meeseek.tools.web.fetch_url", lambda url: "The vendor page text.")
    monkeypatch.setattr(packs, "call", lambda name, args: "Error: `docs/nope.md` does not exist")
    answers["readiness"] = [BLOCKER]
    answers["zoom_out"] = RuntimeError("429 quota")
    doc = ("# Add a cache\nChange src/cache.py as sources/drive/design.md says, see https://vendor.example/ttl. "
           "Update docs/nope.md.")
    rev = goldfish.review(doc, str(tmp_path))
    assert rev.ran == ["comprehension", "readiness"] and "429" in rev.failed["zoom_out"]
    assert rev.refs == ["https://vendor.example/ttl", "src/cache.py", "sources/drive/design.md"]
    assert rev.unread and rev.unread[0].startswith("docs/nope.md")
    assert len(seen) == 3
    for prompt in seen:
        assert doc in prompt and "def get(key)" in prompt and "TTL is 60 s." in prompt
        assert "The vendor page text." in prompt and "docs/nope.md (Error" in prompt
    text = rev.render()
    assert "1 blocker" in text and "Did not run: zoom_out" in text and "not reviewed against: docs/nope.md" in text


def _spec(n: int = 3000) -> str:
    return "# Cache for the reranker\n" + "The reranker caches embeddings. " * (n // 32)


def test_a_short_brief_goes_unreviewed(reviewers, started):
    _, seen = reviewers
    out = claude_tools.ask_claude("看一下這個 repo 怎麼跑測試", cwd="/repo", wait_seconds=10)
    assert seen == [] and started[0][0] == "看一下這個 repo 怎麼跑測試" and out.startswith("[Claude session")


def test_blockers_go_back_once_then_the_brief_goes_with_them_as_trade_offs(reviewers, started, monkeypatch):
    monkeypatch.setattr(ws_mod, "_ticket_projects", lambda: {"PROJ"})
    monkeypatch.setattr(ws_mod, "update_later", lambda *a, **k: None)
    ws = ws_mod.resolve(["PROJ-12"])
    ws_mod.attach_session(ws, "web-1")
    answers, seen = reviewers
    answers["readiness"], answers["zoom_out"] = [BLOCKER], [MAJOR]
    origin = {"session_id": "web-1"}

    out = claude_tools.ask_claude(_spec(), cwd="/repo", wait_seconds=10, _origin=origin)
    assert out.startswith("Nothing went to Claude") and "which module owns the cache" in out
    assert started == [] and len(seen) == 3
    assert "Workstream:" in seen[0]                       # the workstream context is reviewed with the brief

    out = claude_tools.ask_claude(_spec() + "\nThe cache lives in src/cache.py.", cwd="/repo",
                                  wait_seconds=10, _origin=origin)
    assert len(seen) == 6                                  # the one re-review
    [(brief, _)] = started                                 # it went, though a blocker remains
    assert "## Accepted trade-offs (spec review)" in brief and "[blocker] (Plan)" in brief
    assert out.startswith("(Re-review: 1 blocker, 1 major, 0 nit")
    events = [e["text"] for e in ws_mod.get(ws["id"])["events"]]
    assert any("sent back to fix the blockers" in e for e in events)
    assert any(e.startswith("Re-review of the Claude brief") for e in events)

    claude_tools.ask_claude(_spec(), cwd="/repo", wait_seconds=10, _origin=origin)
    assert len(started) == 1                               # a new brief starts over: blockers back again


def test_a_review_that_cannot_run_says_so_and_the_brief_goes(reviewers, started):
    answers, _ = reviewers
    for lens in answers:
        answers[lens] = RuntimeError("no model access")
    out = claude_tools.ask_claude(_spec(), cwd="/repo", wait_seconds=10)
    assert out.startswith("(The spec review did not run: comprehension: no model access")
    assert started[0][0] == _spec()                        # unchanged: no trade-offs pretended


def test_the_threshold_turns_it_off(reviewers, started, _config):
    _config.spec_review.brief_chars = 0
    claude_tools.ask_claude(_spec(), cwd="/repo", wait_seconds=10)
    assert reviewers[1] == [] and started


def test_review_spec_reviews_and_re_reviews_a_document_once(reviewers):
    answers, seen = reviewers
    answers["comprehension"] = [BLOCKER]
    origin = {"session_id": "s-1"}
    first = goldfish.review_spec("# PRD: cache\nbody", _origin=origin)
    assert "1 blocker" in first and "call review_spec once more" in first
    second = goldfish.review_spec("# PRD: cache\nbody, fixed", _origin=origin)
    assert "This was the re-review" in second
    third = goldfish.review_spec("# PRD: cache\nagain", _origin=origin)
    assert "Do not review it again" in third and len(seen) == 6
    answers["comprehension"] = [MAJOR]
    assert "No blockers" in goldfish.review_spec("# Another PRD\nbody", _origin=origin)   # its own count


def test_review_spec_reads_a_saved_document_and_never_fakes_a_review(reviewers, _home):
    answers, _ = reviewers
    (_home.kb / "notes" / "prd.md").write_text("# PRD\nthe text", encoding="utf-8")
    for lens in answers:
        answers[lens] = RuntimeError("down")
    out = goldfish.review_spec(path="notes/prd.md")
    assert out.startswith("The spec review did not run") and "do not claim it was reviewed" in out
    assert goldfish.review_spec(path="../../etc/passwd").startswith("Error")
