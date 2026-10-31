"""Two meeseeks in one process: each its own config, files, channels and hooks."""

import asyncio
import copy

import yaml

from meeseek.app import app, build_app
from meeseek.config import PROJECT_ROOT, Home, build_config, cfg, home
from meeseek.runtime import inbox, messengers


def _app(tmp_path, name):
    raw = yaml.safe_load((PROJECT_ROOT / "project.yaml.example").read_text(encoding="utf-8"))
    raw = copy.deepcopy(raw)
    raw["project"]["name"] = name
    h = Home(root=tmp_path / name, skills=PROJECT_ROOT / "skills")
    h.ensure()
    return build_app(build_config(raw), h)


class _Channel:
    def __init__(self, name):
        self.name = name


def test_two_apps_side_by_side(tmp_path):
    a, b = _app(tmp_path, "alpha"), _app(tmp_path, "beta")

    async def work(x, chat):
        with x.active():
            await asyncio.sleep(0)
            messengers.register(_Channel(chat))
            item = inbox.file(source="chat", key=f"k-{chat}", title=chat, meta={})
            await asyncio.sleep(0)
            return cfg.project.name, home().root.name, sorted(app().channels), inbox.get(item["id"])["title"], \
                [i["title"] for i in inbox.items()]

    async def both():
        return await asyncio.gather(work(a, "phone-a"), work(b, "phone-b"))

    (na, ra, ca, ta, ia), (nb, rb, cb, tb, ib) = asyncio.run(both())
    assert (na, ra, ca, ta, ia) == ("alpha", "alpha", ["phone-a"], "phone-a", ["phone-a"])
    assert (nb, rb, cb, tb, ib) == ("beta", "beta", ["phone-b"], "phone-b", ["phone-b"])
    assert len(a.after_inbox_turn) == len(b.after_inbox_turn) == 2 and a.after_inbox_turn is not b.after_inbox_turn


def test_outside_both_the_process_app_is_untouched(tmp_path):
    before = app()
    with _app(tmp_path, "gamma").active():
        assert cfg.project.name == "gamma"
    assert app() is before and cfg.project.name != "gamma"
