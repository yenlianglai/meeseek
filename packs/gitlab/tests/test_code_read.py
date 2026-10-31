"""search_code and get_file over the git mirror — a real git round trip, offline."""

import subprocess
from types import SimpleNamespace

import pytest

from gitlab_test_settings import use
from meeseek_pack_gitlab import code_read, git_mirror

PROJECT = "group/app"
SOURCE = "\n".join([
    "import os",
    "",
    "def _wear(row, uses):",
    "    row['used'] += uses",
    "",
    *[f"# filler {i}" for i in range(40)],
    "def clean(row):",
    "    _wear(row, -1)",
]) + "\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / "app").mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(src), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (src / "app" / "policy.py").write_text(SOURCE)
    (src / "README.md").write_text("Wear and tear\n")
    run("add", ".")
    run("commit", "-q", "-m", "one")
    sha = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(tmp_path / "remote" / "group" / "app.git")],
                   check=True)
    use(monkeypatch, base_url=f"file://{tmp_path / 'remote'}")
    monkeypatch.setattr(git_mirror, "_env", lambda: None)
    monkeypatch.setattr(git_mirror, "resolve",
                        lambda project, ref: git_mirror.Ref(project, sha, "main", "MR !7 (x)", False))
    monkeypatch.setattr("meeseek_pack_gitlab.gitlab._resolve_project", lambda p: p or PROJECT)
    return SimpleNamespace(sha=sha)


class TestSearch:
    def test_every_match_comes_with_its_line_number_and_context(self, repo):
        out = code_read.search_code("_wear", ref="!7")
        assert out.startswith("**2 match(es) in 1 file(s)**") and "MR !7" in out
        assert "### app/policy.py" in out
        assert "3:def _wear(row, uses):" in out and "47:    _wear(row, -1)" in out
        assert "4-    row['used'] += uses" in out  # context line

    def test_lower_case_matches_any_case_and_upper_case_is_exact(self, repo):
        assert "README.md" in code_read.search_code("wear and tear")
        assert "No matches" in code_read.search_code("WEAR AND TEAR")

    def test_an_invalid_regex_is_searched_literally(self, repo):
        assert "No matches" in code_read.search_code("_wear(row, (")  # unbalanced, literal miss
        assert "47:" in code_read.search_code("_wear(row, -1")

    def test_an_mr_url_names_its_repo_and_mr(self, repo, monkeypatch):
        seen = {}
        monkeypatch.setattr(git_mirror, "resolve", lambda project, ref: seen.update(p=project, r=ref)
                            or git_mirror.Ref(project, repo.sha, "main", "MR !7 (x)", False))
        code_read.search_code("_wear", ref="https://gitlab.x/group/app/-/merge_requests/7")
        assert seen == {"p": "group/app", "r": "!7"}

    def test_path_narrows_the_search(self, repo):
        out = code_read.search_code("wear", path="app/")
        assert "app/policy.py" in out and "README.md" not in out


class TestRead:
    def test_a_file_comes_back_numbered(self, repo):
        out = code_read.get_file("app/policy.py", ref="!7")
        assert "    3  def _wear(row, uses):" in out and "lines 1-47 of 47" in out

    def test_several_windows_in_one_call(self, repo):
        out = code_read.get_file("app/policy.py", ref="!7", ranges="3-4, 46-47")
        assert "lines 3-4:" in out and "lines 46-47:" in out
        assert "   47      _wear(row, -1)" in out and "filler 10" not in out

    def test_a_wrong_path_suggests_the_real_one(self, repo):
        out = code_read.get_file("/policy.py", ref="!7")
        assert out.startswith("Error:") and "`app/policy.py`" in out

    def test_a_bad_range_is_an_error(self, repo):
        assert code_read.get_file("app/policy.py", ranges="here").startswith("Error:")


def test_the_mirror_fetches_with_the_gitlab_token(monkeypatch):
    import base64

    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
    env = git_mirror._env()
    header = env["GIT_CONFIG_VALUE_0"]
    assert header == "Authorization: Basic " + base64.b64encode(b"oauth2:glpat-test").decode()
