"""A picture of an agent's page, for the phone."""

import shutil

import pytest
from PIL import Image

from meeseek.runtime import page_shot


def test_the_empty_band_below_a_short_page_is_cut(tmp_path):
    png = tmp_path / "p.png"
    im = Image.new("RGB", (100, 1000), (13, 16, 20))
    for y in range(100, 300):
        for x in range(10, 90):
            im.putpixel((x, y), (200, 200, 200))
    im.save(png)
    page_shot._trim_bottom(png, margin=20)
    assert Image.open(png).size == (100, 320)


def test_a_page_that_fills_the_window_is_kept_whole(tmp_path):
    png = tmp_path / "p.png"
    im = Image.new("RGB", (100, 400), (13, 16, 20))
    im.putpixel((50, 399), (255, 255, 255))
    im.save(png)
    page_shot._trim_bottom(png)
    assert Image.open(png).size == (100, 400)


@pytest.mark.skipif(not page_shot.chrome(), reason="no Chrome on this machine")
def test_a_page_renders_at_phone_width(tmp_path, monkeypatch):
    page = tmp_path / "p.html"
    page.write_text("<body style='margin:0;background:#123'><div style='height:300px;background:#fff'></div></body>")
    shot = page_shot.screenshot(page)
    assert shot is not None
    w, h = Image.open(shot).size
    assert w == 1000 and h < 1000, "500 CSS px at 2x, the empty band below cut"
    assert page_shot.screenshot(page) == shot, "the same page is not rendered twice"
    shutil.rmtree(page_shot.shots_dir())


def test_a_faint_gradient_below_the_page_counts_as_empty(tmp_path):
    png = tmp_path / "p.png"
    im = Image.new("RGB", (100, 1000), (13, 16, 20))
    for y in range(0, 200):
        for x in range(100):
            im.putpixel((x, y), (220, 220, 220))
    for y in range(200, 1000):   # the background drifts a few levels toward the bottom
        shade = 13 + (y - 200) * 4 // 800
        for x in range(100):
            im.putpixel((x, y), (shade, 16, 20))
    im.save(png)
    page_shot._trim_bottom(png, margin=10)
    assert Image.open(png).size == (100, 210)


def test_old_pictures_are_removed(tmp_path):
    import os
    import time

    page_shot.shots_dir().mkdir(parents=True, exist_ok=True)
    old, new = page_shot.shots_dir() / "old.png", page_shot.shots_dir() / "new.png"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    month_ago = time.time() - 31 * 86400
    os.utime(old, (month_ago, month_ago))
    assert page_shot.prune() == 1
    assert not old.exists() and new.exists()
