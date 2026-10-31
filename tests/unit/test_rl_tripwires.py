"""Tripwires around the learning loop: it proposes, you decide; the grader is not part of the policy."""

import ast
from pathlib import Path

import pytest

from meeseek.context import prompts
from meeseek.runtime import feedback, improve, policy

SRC = Path(__file__).resolve().parents[2] / "src" / "meeseek"
MEMORY_WRITES = {"write_learned_rule", "append_fact", "append_raw", "update_rule", "graduate"}


def _calls_by_function(path: Path) -> dict[str, set[str]]:
    """function name → the names it calls (attribute or bare)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = set()
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    f = node.func
                    names.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
            out[fn.name] = names
    return out


@pytest.mark.parametrize("module", ["runtime/reward.py", "runtime/cases.py", "runtime/improve.py",
                                    "runtime/trajectory.py", "runtime/feedback.py"])
def test_only_an_approved_promotion_writes_learned_memory(module):
    for fn, calls in _calls_by_function(SRC / module).items():
        if calls & MEMORY_WRITES:
            assert (module, fn) == ("runtime/improve.py", "promote"), f"{module}:{fn} writes learned memory"


def test_promote_is_reached_only_through_your_decision():
    callers = [fn for fn, calls in _calls_by_function(SRC / "runtime/improve.py").items() if "promote" in calls]
    assert callers == ["decide"]


def test_a_lesson_that_reads_like_a_score_is_refused(monkeypatch):
    monkeypatch.setattr(improve, "_ask", lambda *a, **k: improve.Lesson(cue="開單", lesson="這樣改的評分比較高"))
    assert improve.reflect("sprint", [], {}, improve.Budget()) is None
    monkeypatch.setattr(improve, "_ask", lambda *a, **k: improve.Lesson(cue="開單", lesson="清單前空一行"))
    assert improve.reflect("sprint", [], {}, improve.Budget())["lesson"] == "清單前空一行"


@pytest.mark.parametrize("target", ["prompt:internal/turn_judge", "prompt:internal/pair_judge", "prompt:internal/proposer",
                                    "prompt:internal/reflector", "prompt:runtime/consent#ask_first",
                                    "prompt:runtime/harness#reconcile", "frontmatter:sprint", "rubric:sprint",
                                    "tool:comment_mr", "tool:jira_create_issue", "tool:propose_sop_rule"])
def test_the_grader_and_the_safety_rails_are_not_editable(target):
    assert not policy.editable(target)


def test_only_your_own_signals_come_in_over_http():
    assert feedback.HUMAN_KINDS == {"thumb", "copy"}
    server = (SRC / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(server)
    kinds = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "record" and len(node.args) >= 3:
            arg = node.args[2]
            if isinstance(arg, ast.Constant):
                kinds.add(arg.value)
    assert kinds and kinds <= feedback.HUMAN_KINDS


@pytest.mark.parametrize("name", ["internal/proposer#rules", "internal/pair_judge#judge",
                                  "internal/reflector#reflect", "internal/turn_judge#judge"])
def test_every_prompt_of_the_loop_reads_its_evidence_as_data(name):
    assert improve.EVIDENCE_BOUNDARY in prompts.source(name)
