"""Meeting coach — a rehearsal before a meeting and a report after it on how
you came across. Only for the meetings `triggers.coach_meetings` names.

Before (with a calendar source): the meeting item asks for a rehearsal
instead of a brief, carrying what the last report said to practise
(`practice_note`). Set the calendar source's `lead_minutes` so there is time to read it.

After: `report()` counts in code what code can from the transcript
(runtime/meeting_transcript.py; your lines marked 我) — share of talk, turns,
the longest, filler words — then the multimodal model reads it with the notes
and rehearsal (and hears the mic track when short enough) and writes the
report. Output: outputs/meetings/<date-time-title>/{report.md, transcript.md,
recording.m4a}.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path


from meeseek.config import cfg
from meeseek.config.paths import home
from meeseek.context import ontology, prompts
from meeseek.runtime.meeting_transcript import Line, cut, duration, plain, render

logger = logging.getLogger(__name__)


def coach_dir() -> Path:
    return home().outputs / "meetings"


AUDIO_KEEP_DAYS = 90   # a coached meeting's recording; its report and transcript stay


def prune_audio(now: float | None = None) -> int:
    """Remove coached meetings' recordings older than AUDIO_KEEP_DAYS; how many."""
    if not coach_dir().is_dir():
        return 0
    cutoff = (now or time.time()) - AUDIO_KEEP_DAYS * 86400
    old = [p for p in coach_dir().glob("*/recording.m4a") if p.stat().st_mtime < cutoff]
    for p in old:
        p.unlink(missing_ok=True)
    return len(old)


LISTEN_MAX_S = 25 * 60      # the coach hears your track for meetings up to this long
FILLERS = ("嗯", "呃", "欸", "那個", "就是", "然後", "其實", "基本上", "所以說", "對對")
_PRACTICE = re.compile(r"^## 下次練習\s*\n(.*?)(?=^## |\Z)", re.S | re.M)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def coached(title: str):
    """The `triggers.coach_meetings` entry the meeting called `title` is, or None."""
    from meeseek.config import cfg

    t = _norm(title)
    return next((m for m in cfg.triggers.coach_meetings if m.name.strip() and _norm(m.name) in t), None)


def learn_note() -> str:
    """The line "向他人學習" opens with when the person has named who they want
    to learn from (`triggers.coach_learn_from`); "" when they have not."""
    from meeseek.config import cfg

    names = [n.strip() for n in cfg.triggers.coach_learn_from if n.strip()]
    return f"特別留意{'、'.join(names)}（使用者指定想學習的對象），但不限於他們。" if names else ""


def focus_note(focus: str) -> str:
    """The line a rehearsal or report gets when its meeting has a focus."""
    return f"這場會的重點：{focus.strip()}\n以這個重點為主軸安排內容。" if focus.strip() else ""


def slug(started: float, title: str) -> str:
    from datetime import datetime

    words = re.sub(r"[^\w一-鿿]+", "-", title.lower()).strip("-")[:40] or "meeting"
    return f"{datetime.fromtimestamp(started):%Y-%m-%d-%H%M}-{words}"


# --- the rehearsal's memory ------------------------------------------------------------

def practice_note(title: str) -> str:
    """What the newest report — of this meeting if there is one — said to practise."""
    if not coach_dir().is_dir():
        return ""
    reports = sorted(coach_dir().glob("*/report.md"), key=lambda p: p.parent.name, reverse=True)
    words = re.sub(r"[^\w一-鿿]+", "-", title.lower()).strip("-")
    same = [r for r in reports if words and words in r.parent.name]
    for r in same + reports:
        m = _PRACTICE.search(r.read_text(encoding="utf-8"))
        if m:
            return f"（{r.parent.name}）\n{m.group(1).strip()}"
    return ""


# --- the numbers ------------------------------------------------------------------------

CHARS_PER_S = 4.0   # speech in Chinese runs about four characters a second


def numbers(lines: list[Line]) -> dict:
    """What the transcript itself says about your part — whichever microphone
    caught you, it counts the same."""
    mine = [ln for ln in lines if ln.who == "我"]
    yours = sum(len(ln.text) for ln in mine)
    everyone = sum(len(ln.text) for ln in lines)
    fillers = {w: sum(ln.text.count(w) for ln in mine) for w in FILLERS}
    return {
        "你說的字數": yours,
        "你的發言比例（依字數）": f"{yours / everyone:.0%}" if everyone else "—",
        "你大約說了（秒）": round(yours / CHARS_PER_S),
        "你發言的段數": len(mine),
        "最長一段（字）": max((len(ln.text) for ln in mine), default=0),
        "口頭禪": {w: n for w, n in fillers.items() if n},
    }


# --- the report ---------------------------------------------------------------------------

def report(folder: Path, lines: list[Line], *, title: str, started: float, notes: str = "",
           rehearsal: str = "", focus: str = "") -> dict:
    """The coach's report on the meeting recorded in `folder`, from its
    transcript `lines`, beside a copy of the transcript and the recording; their
    paths, relative to the repo."""
    from meeseek.runtime.oneshot import ask

    out = coach_dir() / slug(started, title)
    out.mkdir(parents=True, exist_ok=True)
    mixed = folder / "meeting.m4a"
    if mixed.exists():
        shutil.copyfile(mixed, out / "recording.m4a")
    (out / "transcript.md").write_text(render(lines, title), encoding="utf-8")
    nums = numbers(lines)

    files: list[tuple[str, bytes]] = []
    heard = ""
    mic = folder / "mic.m4a"
    if mic.exists() and 0 < duration(mic) <= LISTEN_MAX_S:
        small = cut(mic, 0, LISTEN_MAX_S, out / ".mic-small.m4a")
        if small:
            heard = ("附上的音訊是使用者筆電麥克風的錄音（在會議室時也會錄到別人；用逐字稿裡「我」的時間點找他的聲音，"
                     "聽語速、停頓、語氣與自信）。\n\n")
            files.append(("audio/mp4", small.read_bytes()))
            small.unlink(missing_ok=True)
    prompt = heard + prompts.text(
        "internal/meeting_coach#report", title=title, focus=focus_note(focus),
        learn=learn_note(),
        numbers=json.dumps(nums, ensure_ascii=False, indent=1),
        transcript=plain(lines)[:60000],
        notes=notes[:15000] or "（沒有）", rehearsal=rehearsal[:8000] or "（這場會沒有會前推演）",
        team=ontology.summary()[:4000] or "（還沒有）")
    body = ask(prompt, model=cfg.models.multimodal, files=files, temperature=0.4, max_output_tokens=12000)
    if not body:
        raise ValueError("the coach wrote nothing")
    (out / "report.md").write_text(body + "\n", encoding="utf-8")
    rel = home().rel
    files = {"report": rel(out / "report.md"), "transcript": rel(out / "transcript.md")}
    if (out / "recording.m4a").exists():
        files["recording"] = rel(out / "recording.m4a")
    return files


def rehearsal_for(code: str, title: str, started: float) -> str:
    """The rehearsal the calendar trigger wrote for this meeting, if any: the
    item for the event with this Meet code, or of this title, in the three
    hours before it started."""
    from meeseek.runtime import inbox

    best = None
    for item in inbox.items():
        if item["source"] != "calendar" or not item.get("reply"):
            continue
        meet = item["meta"].get("meet", "")
        near = abs(item.get("received_at", 0) - started) < 3 * 3600
        if (code and code in meet) or (near and item["title"] == title):
            if best is None or item.get("received_at", 0) > best.get("received_at", 0):
                best = item
    return best["reply"] if best else ""
