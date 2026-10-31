"""What an installed meeseek carries besides its code: the templates `meeseek
init` copies, and only the tracked ones — never what a checkout learned."""

import subprocess
import tomllib

from meeseek.config import PROJECT_ROOT


def _force_included() -> dict[str, str]:
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]


def test_the_wheel_carries_exactly_the_tracked_skill_files():
    tracked = subprocess.run(["git", "ls-files", "skills"], cwd=PROJECT_ROOT, capture_output=True, text=True,
                             check=True).stdout.split()
    shipped = sorted(src for src in _force_included() if src.startswith("skills"))
    assert shipped == sorted(tracked), "pyproject's force-include and the tracked skills differ"
    assert not any("learned" in s or "__pycache__" in s for s in shipped)


def test_every_template_lands_under_meeseek_templates():
    for src, dest in _force_included().items():
        assert (PROJECT_ROOT / src).is_file(), src
        assert dest == f"meeseek/_templates/{src}", (src, dest)


def test_init_makes_a_home_and_overwrites_nothing(tmp_path, capsys):
    from meeseek.__main__ import main

    home = tmp_path / "h"
    assert main(["init", "--home", str(home)]) == 0
    assert (home / "project.yaml").exists() and (home / ".env").exists() and (home / "skills" / "code" / "SKILL.md").exists()
    assert not list((home / "skills").rglob("learned.md"))
    (home / "project.yaml").write_text("mine")
    assert main(["init", "--home", str(home)]) == 0
    assert (home / "project.yaml").read_text() == "mine" and "already set up" in capsys.readouterr().out
