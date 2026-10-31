"""One JSONL implementation (JsonlStore): rotation, locking, atomic rewrite,
tolerant reads — and every store goes through it."""

import json
import threading

from meeseek.runtime.store import JsonlStore, path_lock


class TestRoundTrip:
    def test_append_then_read(self, tmp_path):
        s = JsonlStore(tmp_path / "x.jsonl")
        s.append({"a": 1})
        s.append({"b": "中文"})
        assert s.read() == [{"a": 1}, {"b": "中文"}]

    def test_missing_file_reads_empty(self, tmp_path):
        assert JsonlStore(tmp_path / "nope.jsonl").read() == []

    def test_append_creates_the_directory(self, tmp_path):
        s = JsonlStore(tmp_path / "deep" / "nested" / "x.jsonl")
        s.append({"a": 1})
        assert s.read() == [{"a": 1}]

    def test_a_half_written_line_does_not_poison_the_store(self, tmp_path):
        """A process killed mid-write leaves a partial line; the rest must
        still be readable."""
        p = tmp_path / "x.jsonl"
        p.write_text('{"ok": 1}\n\n{"broken": \n{"ok": 2}\n', encoding="utf-8")
        assert JsonlStore(p).read() == [{"ok": 1}, {"ok": 2}]


class TestUpdate:
    def test_update_rewrites_what_the_callback_returns(self, tmp_path):
        s = JsonlStore(tmp_path / "x.jsonl")
        for i in range(3):
            s.append({"i": i})
        s.update(lambda rows: [r for r in rows if r["i"] != 1])
        assert s.read() == [{"i": 0}, {"i": 2}]

    def test_returning_none_changes_nothing(self, tmp_path):
        s = JsonlStore(tmp_path / "x.jsonl")
        s.append({"i": 0})
        s.update(lambda rows: None)
        assert s.read() == [{"i": 0}]

    def test_a_failed_serialisation_leaves_the_store_intact(self, tmp_path):
        """An unserialisable record must not cost the existing rows."""
        s = JsonlStore(tmp_path / "x.jsonl")
        for i in range(3):
            s.append({"i": i})

        try:
            s.update(lambda rows: rows + [{"bad": object()}])
        except TypeError:
            pass

        assert s.read() == [{"i": 0}, {"i": 1}, {"i": 2}]
        assert not list(tmp_path.glob("*.tmp")), "a temp file was left behind"

    def test_rewrite_goes_through_a_temp_file(self, tmp_path):
        """The rename protects against a crash mid-write, which no unit test can
        trigger, so this asserts the mechanism instead."""
        import ast
        import inspect

        src = inspect.getsource(JsonlStore._write_all_unlocked)
        calls = {getattr(n.func, "attr", "") for n in ast.walk(ast.parse(src.strip()))
                 if isinstance(n, ast.Call)}
        assert "replace" in calls, "the rewrite no longer renames a temp file into place"

    def test_a_failing_callback_leaves_the_store_alone(self, tmp_path):
        s = JsonlStore(tmp_path / "x.jsonl")
        s.append({"i": 0})

        def boom(rows):
            raise RuntimeError("crash while computing the new contents")

        try:
            s.update(boom)
        except RuntimeError:
            pass
        assert s.read() == [{"i": 0}]


class TestRotation:
    def test_rotates_past_the_cap(self, tmp_path):
        s = JsonlStore(tmp_path / "x.jsonl", max_bytes=200)
        for i in range(40):
            s.append({"i": i, "pad": "x" * 20})
        assert (tmp_path / "x.jsonl.old").exists(), "nothing was rotated"
        assert s.path.stat().st_size <= 400

    def test_no_cap_means_no_rotation(self, tmp_path):
        s = JsonlStore(tmp_path / "x.jsonl")
        for i in range(50):
            s.append({"i": i, "pad": "y" * 50})
        assert not (tmp_path / "x.jsonl.old").exists()


class TestConcurrency:
    def test_parallel_appends_do_not_interleave(self, tmp_path):
        """The learning judge runs on a background thread while a console turn
        files its own candidates."""
        s = JsonlStore(tmp_path / "x.jsonl")

        def writer(n):
            for i in range(50):
                s.append({"w": n, "i": i, "pad": "z" * 80})

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = s.path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 400
        for line in lines:
            json.loads(line)  # every line is a whole record

    def test_the_lock_is_shared_per_path(self, tmp_path):
        p = tmp_path / "x.jsonl"
        assert path_lock(p) is path_lock(tmp_path / "x.jsonl")
        assert path_lock(p) is not path_lock(tmp_path / "other.jsonl")


def test_no_module_hand_rolls_a_jsonl_writer():
    """Every store goes through JsonlStore, so no store has only a subset of
    the behaviours above."""
    import ast

    from meeseek.config import PROJECT_ROOT

    offenders = []
    for path in (PROJECT_ROOT / "src" / "meeseek").rglob("*.py"):
        if path.name == "store.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # open(..., "a") — an append-mode file handle outside the store
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open"
                    and any(isinstance(a, ast.Constant) and a.value in ("a", "at")
                            for a in node.args)):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"hand-rolled append writer(s): {offenders}"
