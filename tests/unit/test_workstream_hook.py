"""Claude Code's prompt hook (scripts/workstream_hook.py): meeseek's notes on the
work a prompt names, added once and again only when they change."""

import importlib.util
import io
import json
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "scripts" / "workstream_hook.py"


@pytest.fixture
def hook(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("workstream_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "SEEN", tmp_path / "seen")
    monkeypatch.setattr(mod, "_branch", lambda cwd: "feature/PROJ-12-fix")
    return mod


def _run(hook, monkeypatch, capsys, answer):
    asked = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(url, timeout):
        asked.append(url)
        if isinstance(answer, Exception):
            raise answer
        return _Resp(json.dumps(answer).encode())

    monkeypatch.setattr(hook.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "c-1", "cwd": "/repo", "prompt": "fix it"})))
    assert hook.main() == 0
    return capsys.readouterr().out, asked


def test_the_context_is_added_once_and_again_when_it_changes(hook, monkeypatch, capsys):
    ws = {"id": "PROJ-12", "key": "PROJ-12", "context": "## Workstream: fix (PROJ-12)"}
    out, asked = _run(hook, monkeypatch, capsys, ws)
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == ws["context"]
    assert "branch=feature%2FPROJ-12-fix" in asked[0] and "claude_session=c-1" in asked[0]
    out, _ = _run(hook, monkeypatch, capsys, ws)
    assert out == ""                                                     # unchanged: not again
    out, _ = _run(hook, monkeypatch, capsys, {**ws, "context": ws["context"] + "\n- State: merged"})
    assert "merged" in out


def test_nothing_when_meeseek_is_down_or_the_work_is_unknown(hook, monkeypatch, capsys):
    assert _run(hook, monkeypatch, capsys, OSError("connection refused"))[0] == ""
    assert _run(hook, monkeypatch, capsys, {})[0] == ""
