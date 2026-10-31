"""One transcript per meeting, who said what, for everything made from it.

A recording is a folder: app.m4a (what the meeting software played — everyone
else, or the whole meeting for Meet's own recording) and, from the laptop,
mic.m4a (you alone on a headset; the whole room in the office).
`transcript()` hears the tracks window by window and writes one transcript,
your lines marked 我, naming speakers from their voices and the context — how
they are addressed, what they report, the roster and the team's graph — each
window carrying on from the end of the one before. The meeting notes
(context/meeting_notes.py) and the coach's report (runtime/meeting_coach.py)
are both made from it, so a meeting is transcribed once.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from meeseek.config import cfg
from meeseek.context import prompts

logger = logging.getLogger(__name__)

WINDOW_S = 600              # both tracks are cut at the same times, ten minutes a request
# The model's thinking for a window. Measured on ten minutes of a real weekly
# (gemini-3.8-flash, 2026-10-13, three runs each — one run is noise: a single
# default run named a speaker no other run did):
#   default, all captions    44% of lines left as 與會者  US$0.057 a window
#   LOW                      35%                          US$0.050
#   LOW, captions collapsed  39%                          US$0.037
# LOW names speakers as well as the default for half the output tokens.
# MINIMAL is refused by this model.
THINKING: str | None = "LOW"   # Gemini's thinking level; other models ignore it
GAP = "（缺漏）"             # who a window that came back empty is written under
MIN_CHARS = 200             # less said than this is not a meeting worth notes (a test, a wrong tab)
CARRY = 40                  # the last lines of a window, handed to the next so its speakers keep their names


@dataclass
class Line:
    at: str       # mm:ss from the start
    who: str      # 我, another speaker's name, or 與會者
    text: str


class _Turn(BaseModel):
    at: str = Field(description="mm:ss，從這段音訊開頭算")
    who: str = Field(description="我，或其他人的名字；認不出來寫 與會者")
    text: str


class _Turns(BaseModel):
    turns: list[_Turn]


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, timeout=60)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def cut(src: Path, start: int, length: int, dst: Path) -> Path | None:
    """`length` seconds of `src` from `start`, small enough to send inline."""
    if not src.exists() or src.stat().st_size == 0:
        return None
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-t", str(length), "-i", str(src),
                    "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k", str(dst)],
                   check=True, capture_output=True, timeout=600)
    return dst if dst.stat().st_size > 0 else None


def _mmss(s: float) -> str:
    return f"{int(s // 60):02d}:{int(s % 60):02d}"


def _shift(at: str, by: int) -> str:
    try:
        m, s = at.split(":")[-2:]
        return _mmss(int(m) * 60 + int(s) + by)
    except ValueError:
        return _mmss(by)


def transcript(folder: Path, *, roster: str = "", me: str = "", expected: str = "") -> list[Line]:
    """The recording in `folder` as one transcript in order."""
    from meeseek.runtime.oneshot import ask

    mic, app = folder / "mic.m4a", folder / "app.m4a"
    total = max(duration(mic) if mic.exists() else 0, duration(app) if app.exists() else 0)
    from meeseek.context import ontology

    who = ontology.summary()
    if who:
        roster = f"{roster}\n\n從過去會議推斷的角色與關係：\n{who[:3000]}"
    lines: list[Line] = []
    work = folder / ".transcript"
    work.mkdir(exist_ok=True)
    try:
        for start in range(0, int(total) + 1, WINDOW_S):
            files: list[tuple[str, bytes]] = []
            labels: list[str] = []
            you = cut(mic, start, WINDOW_S, work / f"mic-{start}.m4a")
            them = cut(app, start, WINDOW_S, work / f"app-{start}.m4a")
            if you:
                labels.append("音訊 1：使用者筆電的麥克風")
                files.append(("audio/mp4", you.read_bytes()))
            if them:
                labels.append("音訊 2：會議軟體傳來的聲音" if you else "會議的錄音（所有人）")
                files.append(("audio/mp4", them.read_bytes()))
            if not files:
                continue
            # The window before, as written: who it named, the voices it named them by.
            before = plain(lines[-CARRY:]) if lines else ""
            prompt = ("附上的音訊依序是：" + "；".join(labels) + "。\n\n"
                      + prompts.text("internal/meeting_transcript", roster=roster or "（沒有名單）",
                                     me=me or "（不知道）", expected=expected[:3000] or "（沒有會前推演）",
                                     before=before or "（這是會議的開頭）"))
            # A window that comes back empty is asked once more; one that still
            # does is a gap in the transcript, not the end of the meeting's notes
            # (2026-10-21: one empty window failed a two-hour meeting).
            turns, why = None, ""
            for _ in range(2):
                try:
                    # Ten minutes of two audio tracks a request: longer than the default wait.
                    turns = ask(prompt, model=cfg.models.multimodal, files=files, schema=_Turns, temperature=0.1,
                                max_output_tokens=30000, thinking=THINKING, timeout_s=900).turns
                    break
                except ValueError as e:      # empty, cut off, refused: not JSON for _Turns
                    why = str(e)[:200]
            if turns is None:
                logger.warning("transcript of %s: nothing came back for %s–%s (%s)", folder.name, _mmss(start),
                               _mmss(start + WINDOW_S), why)
                lines.append(Line(_mmss(start), GAP, f"（{_mmss(start)} 起的這段沒有轉出逐字稿）"))
                continue
            lines += [Line(_shift(t.at, start), t.who.strip() or "與會者", t.text.strip())
                      for t in turns if t.text.strip()]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return lines


def said(lines: list[Line]) -> int:
    """How much was said, in characters (a gap's marker is not speech)."""
    return sum(len(ln.text) for ln in lines if ln.who != GAP)


def plain(lines: list[Line]) -> str:
    """The transcript as the models read it: one line per turn."""
    return "\n".join(f"{ln.at} {ln.who}：{ln.text}" for ln in lines)


def render(lines: list[Line], title: str) -> str:
    """The transcript as a person reads it."""
    rows = [f"**{ln.at} {ln.who}**：{ln.text}" for ln in lines]
    return (f"# 逐字稿：{title}\n\n誰說的，是從聲音和上下文推斷的；「我」是你。"
            "在會議室裡一支麥克風聽到所有人時，可能認錯。\n\n" + "\n\n".join(rows) + "\n")
