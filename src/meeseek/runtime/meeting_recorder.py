"""Meeting recording — while a Google Meet tab is open in Chrome, record it.

The server ticks every TICK_SECONDS (`triggers.record_meetings`, macOS only):

  1. Chrome's tabs are read over AppleScript; a meet.google.com/<code> tab is
     a meeting you are in. A full-screen Chrome window is missing from that
     list entirely (and a minimized one flickers out of it), so a Chrome
     window whose System Events TITLE names an in-progress calendar meeting
     counts too (`window_meet_codes`, 2026-10-23: a meeting joined full screen
     was never recorded). When the tabs cannot be read at all (Automation not
     allowed yet), a calendar event with a Meet link in progress counts instead.
  2. recorder/meeting-recorder (ScreenCaptureKit) records Chrome's audio and
     your microphone into recordings_dir()/<date>-<code>/, and an Inbox item
     says so (named from the calendar when the login can read it). Each tick
     also asks the tab whether it is still in the call (`in_call`).
  3. When the tab has been gone GONE_GRACE_S, the recorder is stopped and the
     meeting paused: back within RESUME_S, it goes on in the same item, as a
     new part (so does a recorder that stopped on its own). Past the calendar
     event's end by MAX_AFTER_END_S with the tab not seen still in the call (a
     meeting that runs over goes on being recorded), or past MAX_RECORDING_S,
     it is over: not started again from a tab left open. Then the parts are
     joined, the tracks
     are mixed into meeting.m4a (kept), the meeting is transcribed once
     (runtime/meeting_transcript.py), and notes go into
     knowledge_base/sources/meetings; a near-silent recording gets none. Who
     is who goes into the team's graph (context/ontology.py).
  4. For a coached meeting (`triggers.coach_meetings`), the coach writes its
     report (runtime/meeting_coach.py).
  5. Your and your team's action items are searched in Jira and proposed
     as comments or new tickets, held on the item (runtime/meeting_tickets.py).
  6. The item's agent turn lists decisions and to-dos from the notes; any
     write waits for your yes like every Inbox write.

macOS asks once for Screen & System Audio Recording, Microphone and
Automation (Chrome) access for the app the server runs in; until granted the
item says what to allow.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import select
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from meeseek.config import PROJECT_ROOT
from meeseek.config.paths import home
from meeseek.context import prompts
from meeseek.runtime import inbox
from meeseek.runtime.llm import failure_message

logger = logging.getLogger(__name__)

RECORDER = PROJECT_ROOT / "recorder" / "meeting-recorder"
_SOURCE = PROJECT_ROOT / "recorder" / "MeetingRecorder.swift"


def recordings_dir() -> Path:
    return home().data / "recordings"


TICK_SECONDS = 15           # how soon a recording starts and stops after the tab opens or closes
MAX_RECORDING_S = 3 * 3600
MAX_AFTER_END_S = 10 * 60
_RETRY_AFTER_S = 60        # a code whose recording failed to start is retried after this: a
                           # meeting is short, and the usual cause (a macOS prompt) passes
_START_WAIT_S = 20
GONE_GRACE_S = 45          # a Meet tab missing from this many seconds of tab lists is closed; one
                           # unread list is not the end of a meeting (2026-10-20: a planning
                           # meeting cut into 34 recordings, a tick at a time)
RESUME_S = 5 * 60          # a meeting whose tab is back within this goes on in the same recording
# A Meet tab's code, after any path Meet puts before it: meet.google.com/abc-defg-hij,
# and meet.google.com/meet/abc-defg-hij (2026-10-21: a daily joined that way was never recorded).
_MEET = re.compile(r"https://meet\.google\.com/(?:[\w-]+/)*?([a-z]{3}-[a-z]{4}-[a-z]{3})(?![\w-])")
_TABS = ('if application "Google Chrome" is running then '
         'tell application "Google Chrome" to get URL of every tab of every window')

_proc: subprocess.Popen | None = None
_failed: dict[str, float] = {}


_AUTOMATION_HINT = ("Chrome's tabs are unreadable: macOS has not let the server's app control Google "
                    "Chrome. Allow it in System Settings → Privacy & Security → Automation (under, say, "
                    "Visual Studio Code); if the app is not listed, run tccutil reset AppleEvents to be asked again")

# Why the last tick found meetings by the calendar instead of Chrome's tabs; "" when it did not.
fallback_reason = ""


def meet_codes() -> list[str]:
    """Meeting codes of the Meet tabs open in Chrome (none when Chrome is not
    running). A full-screen Chrome window is missing from this list entirely,
    and a minimized one flickers out of it — `window_meet_codes` covers those
    through the windows' titles."""
    try:
        out = subprocess.run(["osascript", "-e", _TABS], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Chrome's tabs are unreadable: macOS is waiting for you to allow controlling Google Chrome") from None
    if out.returncode != 0:
        if "-1743" in out.stderr:
            raise RuntimeError(_AUTOMATION_HINT)
        raise RuntimeError(out.stderr.strip() or "osascript failed")
    return sorted(set(_MEET.findall(out.stdout)))


def _calendar_meetings(now: float | None = None) -> list[tuple[str, str]]:
    """(code, title) of the calendar's meetings in progress."""
    from meeseek.runtime import packs
    from meeseek.runtime.triggers import _iso

    now = now or time.time()
    events = packs.call_json("meetings", {"time_min": _iso(now), "time_max": _iso(now + 60)})["items"]
    return sorted({(ev["code"], (ev.get("title") or "").strip())
                   for ev in events if ev.get("code") and ev.get("start") and not ev.get("declined")})


def calendar_codes(now: float | None = None) -> list[str]:
    """Meet codes of calendar events in progress — how meetings are found when
    Chrome's tabs cannot be read."""
    return sorted({code for code, _ in _calendar_meetings(now)})


# Chrome's window titles, one per line — System Events sees the windows the tab
# list misses. Joined by linefeed, not osascript's ", ": a meeting title may
# hold a comma.
_WINDOW_NAMES = '''
on run
  if application "Google Chrome" is not running then return ""
  set txt to ""
  tell application "System Events" to tell process "Google Chrome"
    repeat with w in windows
      set txt to txt & (name of w) & linefeed
    end repeat
  end tell
  return txt
end run
'''


def _window_names() -> list[str]:
    out = subprocess.run(["osascript", "-e", _WINDOW_NAMES], capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "osascript failed")
    return [n for n in out.stdout.splitlines() if n.strip()]


_TITLE_CAL_S = 60
_title_cal: tuple[float, list[tuple[str, str]]] = (0.0, [])
# synthetic code → the meeting name its window title showed, for the item's name
_title_names: dict[str, str] = {}
# what Chrome puts after the meeting's name in a Meet window's title: the mic
# and camera state (localised), then its own suffix
_TITLE_CUTS = (" - 麥克風", " - 相機", " - 攝影機", " - Microphone", " - Camera", " - Google Chrome")


def _meet_window_name(title: str) -> str:
    """The meeting's own name in a Chrome Meet window's title — after
    "Meet - ", before the mic/camera state and the browser's suffix — or ""
    when the title is not a Meet window's."""
    if not title.startswith("Meet - "):
        return ""
    name = title[len("Meet - "):]
    for cut in _TITLE_CUTS:
        i = name.find(cut)
        if i > 0:
            name = name[:i]
    return name.strip()


def _title_code(name: str) -> str:
    """A stable key for a meeting known only by its window title: not a Meet
    code, but it flows through the same paths (the folder, the item's key)."""
    return "ttl-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:9]


def window_meet_codes(tab_codes: list[str] = (), now: float | None = None) -> list[str]:  # type: ignore[assignment]
    """Codes of meetings a Chrome window's TITLE says are open but the tab list
    misses. A full-screen Chrome window does not appear in AppleScript's
    `every tab of every window` at all (2026-10-23: a meeting joined full
    screen was never recorded — AppleScript saw one window, System Events
    four, the meeting's full screen among them), and a minimized one flickers
    out of the list. The title has no meet code, so a window named for Meet is
    matched to an in-progress calendar event by the event's title appearing in
    the window's (the calendar answer is kept for a minute, not asked per
    tick). A Meet window on no calendar — a meeting joined from a pasted link —
    still records: it gets a key from its title (`ttl-…`), but only while no
    real code is open anywhere, so a visible tab's meeting is never doubled.
    Needs Accessibility for System Events; silence — [] — when macOS does not
    allow it."""
    global _title_cal
    try:
        names = _window_names()
    except Exception as e:  # noqa: BLE001 — without the permission the tab list stands alone, as before
        logger.debug("[recorder] window titles unreadable: %s", e)
        return []
    meetish = [n for n in names if n.startswith("Meet - ") or "Google Meet" in n]
    if not meetish:
        return []
    now = now or time.time()
    if now - _title_cal[0] > _TITLE_CAL_S:
        try:
            _title_cal = (now, _calendar_meetings(now))
        except Exception as e:  # noqa: BLE001 — no calendar: the title alone still names a key below
            logger.debug("[recorder] calendar unreadable for the title match: %s", e)
            _title_cal = (now, [])
    matched = sorted({code for code, title in _title_cal[1]
                      if title and any(title in n for n in meetish)})
    if matched or tab_codes:
        return matched
    out = []
    for n in meetish:
        name = _meet_window_name(n)
        if name:
            code = _title_code(name)
            _title_names[code] = name
            out.append(code)
    return sorted(set(out))


def open_meetings() -> list[str]:
    """The meetings you are in: Chrome's Meet tabs — plus the ones only a
    window title betrays (full screen) — or, failing both, the calendar."""
    global fallback_reason
    try:
        codes = meet_codes()
        fallback_reason = ""
    except RuntimeError as tabs:
        fallback_reason = str(tabs)
        try:
            return calendar_codes()
        except Exception as cal:  # noqa: BLE001
            from meeseek.runtime.triggers import _explain as explain

            raise RuntimeError(f"{tabs}; the calendar is unreadable too: {explain(cal)}") from None
    return sorted({*codes, *window_meet_codes(codes)})


def _calendar_event(code: str) -> dict:
    """The calendar's meeting with this code: {"title", "end", "me"}; {} if
    there is none or the calendar cannot be read."""
    from meeseek.runtime import packs
    from meeseek.runtime.triggers import _iso, _ts

    try:
        now = time.time()
        events = packs.call_json("meetings", {"time_min": _iso(now - 3 * 3600), "time_max": _iso(now + 3600)})["items"]
    except Exception:  # noqa: BLE001 — the name is a nicety; recording goes on without it
        return {}
    for ev in events:
        if ev.get("code") == code:
            return {"title": ev.get("title", ""), "end": _ts(ev["end"]) if ev.get("end") else 0, "me": ev.get("me", "")}
    return {}


def _build() -> None:
    """Compile the recorder on first use."""
    if RECORDER.exists() and RECORDER.stat().st_mtime >= _SOURCE.stat().st_mtime:
        return
    if not shutil.which("swiftc"):
        raise RuntimeError("swiftc not found: install the Xcode Command Line Tools (xcode-select --install)")
    subprocess.run(["swiftc", "-O", "-o", str(RECORDER), str(_SOURCE)], check=True,
                   capture_output=True, text=True, timeout=600)


_PERMISSION_HINT = ("macOS has not allowed recording: in System Settings → Privacy & Security, allow "
                    "the server's app (Terminal or VS Code) under Screen & System Audio Recording and "
                    "Microphone, then restart it")


def _explain(stderr: str) -> str:
    if "TCC" in stderr or "-3801" in stderr or "not authorized" in stderr.lower():
        return _PERMISSION_HINT
    return stderr.strip()[-300:] or "The recorder did not start"


def _spawn(folder: Path) -> tuple[subprocess.Popen | None, str]:
    """Start the recorder into `folder`: (its process, "") once it says it is
    recording, or (None, what it said). What it says later — why it stopped on
    its own — is kept in folder/recorder.log."""
    _build()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "recorder.log").open("a") as log:   # the child keeps its own copy of the handle
        proc = subprocess.Popen([str(RECORDER), str(folder)], stdout=subprocess.PIPE, stderr=log, text=True)
    # Wait for "recording" without blocking on it: a permission dialog nobody
    # answers keeps the recorder silent.
    deadline = time.time() + _START_WAIT_S
    line = ""
    while proc.poll() is None and (left := deadline - time.time()) > 0:
        ready, _, _ = select.select([proc.stdout], [], [], left)
        if ready:
            line = proc.stdout.readline().strip()
            if line == "recording":
                return proc, ""
    if proc.poll() is None:
        proc.kill()
        proc.wait(5)
    return None, _log_tail(folder)


def _log_tail(folder: Path) -> str:
    try:
        return (folder / "recorder.log").read_text(encoding="utf-8")[-2000:]
    except OSError:
        return ""


def _start(code: str) -> None:
    """Start recording a meeting and file its item."""
    global _proc
    event = _calendar_event(code)
    folder = recordings_dir() / f"{datetime.now():%Y%m%d-%H%M}-{code}"
    # a meeting known only by its window title (ttl-…) is named by that title
    title = event.get("title") or _title_names.get(code) or f"Google Meet {code}"
    meta = {"code": code, "dir": str(folder), "pid": 0,
            "started": time.time(), "end": event.get("end", 0), "me": event.get("me", "")}
    # A retry after a failed start is the same meeting: one item, not one per attempt.
    item = next((i for i in inbox.items() if i["source"] == "meeting" and i["status"] == "failed"
                 and i["meta"].get("code") == code and time.time() - i.get("received_at", 0) < 3 * 3600), None)
    if item:
        item["meta"].update(meta)
        item["error"] = ""
        inbox.save(item)
    else:
        item = inbox.file(source="meeting", key=f"meeting:{folder.name}", title=f"Recording: {title}",
                          link="" if code.startswith("ttl-") else f"https://meet.google.com/{code}", meta=meta)
    proc, err = _spawn(folder)
    item = inbox.get(item["id"]) or item
    if proc is None:
        item["status"], item["error"] = "failed", _explain(err)
        # A missing permission does not fix itself by the next retry: one item per meeting is enough.
        _failed[code] = float("inf") if item["error"] == _PERMISSION_HINT else time.time()
        inbox.save(item)
        logger.warning("[recorder] %s did not start: %s", code, err.strip()[:200])
        return
    _proc = proc
    item["meta"]["pid"] = proc.pid
    item["status"] = "recording"
    inbox.save(item)
    logger.info("[recorder] recording %s into %s", code, folder)


def _resume(item: dict) -> None:
    """Go on recording a meeting in its own item: a new part beside the
    first, joined to it before the meeting is transcribed (`join_parts`)."""
    global _proc
    meta = item["meta"]
    parts = meta.get("parts") or []
    name = f"part{len(parts) + 2}"
    proc, err = _spawn(Path(meta["dir"]) / name)
    item = inbox.get(item["id"]) or item
    item["meta"].pop("paused_at", None)
    if proc is None:   # what it has is finished on the next tick
        item["status"] = "recorded"
        item["meta"]["stopped"] = f"could not go on recording: {_explain(err)}"
        inbox.save(item)
        logger.warning("[recorder] %s could not resume: %s", meta["code"], err.strip()[:200])
        return
    _proc = proc
    item["meta"]["parts"] = parts + [{"dir": name, "started": time.time()}]
    item["meta"]["pid"] = proc.pid
    item["status"] = "recording"
    inbox.save(item)
    logger.info("[recorder] %s goes on recording into %s", meta["code"], name)


def _stop(pid: int) -> None:
    """Ask the recorder to finish its files, and wait for it."""
    if pid <= 0:
        return  # never 0 or negative: that signals a whole process group
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        return
    for _ in range(60):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.5)


def join_parts(folder: Path, parts: list[dict]) -> None:
    """A meeting recorded in parts (the recorder stopped, the tab dropped for
    a moment) as one: each track of the first part and the later ones, in
    order, into folder/<track>. The first part's own files move to part1/, so
    joining again after a failed finish gives the same result."""
    if not parts:
        return
    first = folder / "part1"
    first.mkdir(exist_ok=True)
    for track in ("app.m4a", "mic.m4a"):
        if (folder / track).exists() and not (first / track).exists():
            (folder / track).rename(first / track)
        pieces = [d / track for d in [first, *(folder / p["dir"] for p in parts)]
                  if (d / track).exists() and (d / track).stat().st_size > 0]
        if not pieces:
            continue
        inputs = [a for t in pieces for a in ("-i", str(t))]
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex",
                        "".join(f"[{i}:a]" for i in range(len(pieces))) + f"concat=n={len(pieces)}:v=0:a=1",
                        "-ac", "1", "-c:a", "aac", "-b:a", "64k", str(folder / track)],
                       check=True, capture_output=True, text=True, timeout=1800)


def mix(folder: Path) -> Path:
    """app.m4a + mic.m4a → meeting.m4a (mono, 16 kHz — what transcription needs)."""
    tracks = [p for p in (folder / "app.m4a", folder / "mic.m4a") if p.exists() and p.stat().st_size > 0]
    if not tracks:
        raise RuntimeError("Nothing was recorded")
    out = folder / "meeting.m4a"
    inputs = [a for t in tracks for a in ("-i", str(t))]
    mixing = (["-filter_complex", f"amix=inputs={len(tracks)}:duration=longest:normalize=0"]
              if len(tracks) > 1 else [])
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, *mixing,
                    "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "48k", str(out)],
                   check=True, capture_output=True, text=True, timeout=1800)
    return out


async def _finish(item: dict) -> None:
    """Mix, transcribe once, make the notes (and for a coached meeting the
    coach's report) from the transcript, and hand them to the agent. A
    recording in which almost nothing is said — a test, a tab opened by
    mistake — is set aside without notes."""
    from meeseek.context.meeting_notes import _load_team_context, notes_from_transcript
    from meeseek.runtime import meeting_coach
    from meeseek.runtime.meeting_transcript import MIN_CHARS, plain, render, said, transcript

    item["status"] = "working"
    inbox.save(item)
    meta = item["meta"]
    folder = Path(meta["dir"])
    title = item["title"].removeprefix("Recording: ")
    coaching = meeting_coach.coached(title)
    rehearsal = meeting_coach.rehearsal_for(meta.get("code", ""), title, meta["started"]) if coaching else ""
    name = f"{datetime.fromtimestamp(meta['started']):%Y-%m-%d %H%M} {title}"
    try:
        await asyncio.to_thread(join_parts, folder, meta.get("parts") or [])
        audio = await asyncio.to_thread(mix, folder)
        item["meta"]["audio"] = str(audio)
        inbox.save(item)
        lines = await asyncio.to_thread(transcript, folder, roster=_load_team_context(), me=meta.get("me", ""),
                                        expected=rehearsal)
        if said(lines) < MIN_CHARS:
            item = inbox.get(item["id"]) or item
            item["status"] = "ignored"
            item["reply"] = (f"這段錄音幾乎沒有人說話（{said(lines)} 字），像是測試或誤開的分頁，"
                             "所以沒有寫會議記錄。錄音還在：" + str(audio))
            inbox.save(item)
            return
        notes_path = await asyncio.to_thread(
            notes_from_transcript, plain(lines), name,
            date=f"{datetime.fromtimestamp(meta['started']):%Y-%m-%d}", transcript_md=render(lines, title))
    except Exception as e:  # noqa: BLE001
        logger.exception("[recorder] finishing %s failed", item["id"])
        item = inbox.get(item["id"]) or item
        item["status"], item["error"] = "failed", failure_message(e)
        inbox.save(item)
        from meeseek.runtime import messengers

        await messengers.meeting_failed(item)
        return
    notes = notes_path.read_text(encoding="utf-8")
    item = inbox.get(item["id"]) or item
    item["body"] = notes[:8000]
    item["meta"]["notes"] = home().rel(notes_path)
    inbox.save(item)
    await _learn_who_is_who(item, lines, notes, title)
    jira = await _tickets(item, notes, title)
    item = inbox.get(item["id"]) or item
    common = prompts.text("runtime/trigger#common")
    coached = await _coach(item, lines, title, notes, rehearsal, coaching.focus) if coaching else None
    if coached:
        files, report = coached
        item = inbox.get(item["id"]) or item
        item["meta"]["coach"] = files
        item["message"] = prompts.text("runtime/trigger#meeting_coached", common=common, title=title,
                                       files="\n".join(files.values()), report=report[:12000],
                                       notes=notes[:30000], jira=jira)
    else:
        item["message"] = prompts.text("runtime/trigger#meeting", common=common,
                                       title=item["title"], notes=notes[:30000], jira=jira)
    inbox.save(item)
    await inbox.work(item["id"], item["message"])


async def _tickets(item: dict, notes: str, title: str) -> str:
    """Your and your team's action items, searched in Jira and held on the
    item as proposals; what the turn is told about Jira. When that is off or
    fails, the turn proposes what it finds itself."""
    from meeseek.context.meeting_notes import _load_team_context
    from meeseek.runtime import meeting_tickets

    if not meeting_tickets.enabled():
        return prompts.text("runtime/trigger#meeting_jira_free")
    meta = item["meta"]
    try:
        listed = await meeting_tickets.propose(
            item, notes, title=title, date=f"{datetime.fromtimestamp(meta['started']):%Y-%m-%d}",
            me=meta.get("me", ""), team_context=_load_team_context())
    except Exception as e:  # noqa: BLE001 — the notes stand; the turn proposes on its own
        logger.exception("[recorder] Jira proposals for %s failed", item["id"])
        item = inbox.get(item["id"]) or item
        item["meta"]["tickets_error"] = failure_message(e)
        inbox.save(item)
        return prompts.text("runtime/trigger#meeting_jira_free")
    return prompts.text("runtime/trigger#meeting_jira_held", tickets=meeting_tickets.summary(listed))


async def _learn_who_is_who(item: dict, lines: list, notes: str, title: str) -> None:
    """What this meeting shows about who is who, into the team's graph
    (context/ontology.py); a failure here costs only that."""
    from meeseek.context import ontology
    from meeseek.runtime.meeting_transcript import plain

    meta = item["meta"]
    try:
        await asyncio.to_thread(ontology.learn, plain(lines), notes, title=title,
                                date=f"{datetime.fromtimestamp(meta['started']):%Y-%m-%d}", me=meta.get("me", ""))
    except Exception:  # noqa: BLE001
        logger.exception("[recorder] reading who is who in %s failed", item["id"])


async def _coach(item: dict, lines: list, title: str, notes: str, rehearsal: str,
                 focus: str = "") -> tuple[dict, str] | None:
    """The coach's report on this meeting, from its transcript: its files and
    text, or None when it could not be made (the notes go on without it; the
    item says why)."""
    from meeseek.runtime import meeting_coach

    meta = item["meta"]
    try:
        files = await asyncio.to_thread(meeting_coach.report, Path(meta["dir"]), lines, title=title,
                                        started=meta["started"], notes=notes, rehearsal=rehearsal, focus=focus)
        report = (home().root / files["report"]).read_text(encoding="utf-8")
        try:
            from meeseek.context import personal

            await asyncio.to_thread(personal.note_practice, report,
                                    f"會後報告 {title} {datetime.fromtimestamp(meta['started']):%Y-%m-%d}")
        except Exception:  # noqa: BLE001 — the report is made; remembering what to practise can wait
            logger.exception("[recorder] noting what to practise from %s failed", item["id"])
        return files, report
    except Exception as e:  # noqa: BLE001
        logger.exception("[recorder] coaching %s failed", item["id"])
        item = inbox.get(item["id"]) or item
        item["meta"]["coach_error"] = failure_message(e)
        inbox.save(item)
        return None


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# Transcriptions in flight. They run beside the ticks, not in them: a tick that
# waited minutes for one would miss the next meeting starting.
_finishing: set[asyncio.Task] = set()


def _finish_later(item: dict) -> None:
    item["status"] = "working"  # claimed now, so no later tick finishes it twice
    inbox.save(item)
    task = asyncio.get_running_loop().create_task(_finish(item))
    _finishing.add(task)
    task.add_done_callback(_finishing.discard)


async def tick() -> None:
    """Start, keep or stop the recording to match the open Meet tabs."""
    global _proc, _pruned_at
    if time.time() - _pruned_at > 6 * 3600:
        _pruned_at = time.time()
        await asyncio.to_thread(prune)
    for item in inbox.items():
        if item["source"] == "meeting" and item["status"] == "recorded":
            _finish_later(item)

    meetings = [i for i in inbox.items() if i["source"] == "meeting"]
    current = next((i for i in meetings if i["status"] == "recording"), None)
    paused = [i for i in meetings if i["status"] == "paused"]
    try:
        codes = await asyncio.to_thread(open_meetings)
    except RuntimeError:
        if current is None and not paused:
            raise
        codes = None  # cannot tell whether the meeting goes on: only the time limits apply
    now = time.time()
    for item in paused:
        back = codes is not None and item["meta"]["code"] in codes
        if current is None and back and now - item["meta"]["paused_at"] < RESUME_S:
            await asyncio.to_thread(_resume, item)
            return
        if now - item["meta"]["paused_at"] >= RESUME_S:
            _finish_later(item)
    if current:
        # Asked first: past the calendar's end, a tab seen still in the call
        # keeps it going — meetings run over. Only a positive sighting counts;
        # unknown (Chrome not running the probe, Meet's page changed) stops.
        await _watch_call(current)
        current = inbox.get(current["id"]) or current
        meta = current["meta"]
        past_end = bool(meta.get("end")) and now > meta["end"] + MAX_AFTER_END_S
        if past_end and codes is not None and meta.get("in_call") is True:
            past_end = False
        if now - meta["started"] > MAX_RECORDING_S or past_end:
            await _end(current, "past the time limit" if not past_end else "past the calendar's end", final=True)
            return
        if codes is not None and meta["code"] not in codes:
            # Absence from Chrome's window list is weak evidence: a minimized
            # window drops out of `every tab of every window` while the call
            # goes on (2026-10-22: a daily cut in two — the person never left,
            # they shared their screen and worked elsewhere). While the
            # calendar says the meeting is on, keep recording: the calendar's
            # end and the time limit above still stop it.
            if meta.get("end") and now < meta["end"]:
                if not meta.get("tab_unseen"):
                    meta["tab_unseen"] = now
                    inbox.save(current)
                    logger.info("[recorder] %s not in Chrome's window list; the calendar says the "
                                "meeting is on — recording goes on", meta["code"])
                if not _alive(meta["pid"]):
                    await asyncio.to_thread(_resume, current)
                return
            if not meta.get("gone_since"):
                meta["gone_since"] = now
                inbox.save(current)
            if now - meta["gone_since"] >= GONE_GRACE_S:
                await _end(current, "its tab closed", final=False)
            return
        gone, unseen = meta.pop("gone_since", None), meta.pop("tab_unseen", None)
        if gone or unseen:
            inbox.save(current)
        if not _alive(meta["pid"]):
            # Stopped on its own, the tab still open: go on in a new part.
            logger.warning("[recorder] %s stopped on its own: %s", meta["code"],
                           _log_tail(Path(meta["dir"]) / (meta["parts"][-1]["dir"] if meta.get("parts") else "")).strip()[-300:])
            await asyncio.to_thread(_resume, current)
        return
    assert codes is not None
    for code in [c for c in _ended if c not in codes]:
        del _ended[code]   # its tab closed: the next time it opens is a new meeting
    fresh = [c for c in codes if time.time() - _failed.get(c, 0) > _RETRY_AFTER_S]
    fresh = [c for c in fresh if c not in _ended or await _rejoined(c)]
    if fresh:
        await asyncio.to_thread(_start, fresh[0])


# Meetings ended past their calendar's end (or the time limit) whose tab is
# still open — on "you left the meeting", usually. Not started again until the
# tab closes, or is seen back in the call (2026-10-13: one started and stopped
# every minute for 53 minutes).
_ended: dict[str, float] = {}


async def _rejoined(code: str) -> bool:
    try:
        on = await asyncio.to_thread(in_call, code)
    except Exception:  # noqa: BLE001 — cannot tell: do not start it again
        return False
    if on:
        del _ended[code]
    return bool(on)


async def _end(item: dict, reason: str, *, final: bool) -> None:
    """Stop the recorder. A final stop (a time limit) finishes the meeting; a
    closed tab pauses it for RESUME_S, in case the meeting comes back."""
    global _proc
    meta = item["meta"]
    await asyncio.to_thread(_stop, meta["pid"])
    _proc = None
    item = inbox.get(item["id"]) or item
    item["meta"]["stopped"] = reason
    item["meta"].pop("gone_since", None)
    item["meta"].pop("tab_unseen", None)
    logger.info("[recorder] %s stopped: %s", meta["code"], reason)
    if final:
        _ended[meta["code"]] = time.time()
        _finish_later(item)
    else:
        item["status"], item["meta"]["paused_at"] = "paused", time.time()
        inbox.save(item)


# Runs in the Meet tab: its control bar (leaving, the captions toggle) is there
# while in the call; the page left open after it ("you left the meeting") has neither.
_IN_CALL_JS = r"""[...document.querySelectorAll("button[aria-label]")].some((b) =>
  /^(leave call|退出通話|離開通話|關閉字幕|開啟字幕|turn (on|off) captions)(\s*\(c\))?$/i
    .test(b.getAttribute("aria-label").trim())) ? "yes" : "no"
"""
_IN_CALL = '''
on run argv
  tell application "Google Chrome"
    repeat with w in windows
      repeat with t in tabs of w
        if URL of t contains "meet.google.com/" and URL of t contains (item 1 of argv) then return execute t javascript (item 2 of argv)
      end repeat
    end repeat
  end tell
  return ""
end run
'''
_JS_HINT = ("Chrome does not run JavaScript from Apple Events, so a meeting running past its calendar end "
            f"stops being recorded {MAX_AFTER_END_S // 60} minutes after it: in Chrome, "
            "View → Developer → Allow JavaScript from Apple Events")


def in_call(code: str) -> bool | None:
    """Whether the Meet tab for `code` is still in the call; None when there
    is no such tab. Raises RuntimeError with what to allow when Chrome will
    not run it."""
    out = subprocess.run(["osascript", "-e", _IN_CALL, code, _IN_CALL_JS],
                         capture_output=True, text=True, timeout=20)
    if out.returncode != 0:
        err = out.stderr.strip()
        if "(12)" in err or "JavaScript" in err:
            raise RuntimeError(_JS_HINT)
        raise RuntimeError(err or "osascript failed")
    return {"yes": True, "no": False}.get(out.stdout.strip())


async def _watch_call(item: dict) -> None:
    """Note on the item whether its tab is still in the call (see `tick`)."""
    meta = item["meta"]
    try:
        on, error = await asyncio.to_thread(in_call, meta["code"]), ""
    except Exception as e:  # noqa: BLE001 — a help for meetings running over; the recording goes on without it
        on, error = None, str(e)
    if on != meta.get("in_call") or error != meta.get("call_error", ""):
        item = inbox.get(item["id"]) or item
        item["meta"]["in_call"], item["meta"]["call_error"] = on, error
        inbox.save(item)


def recover() -> None:
    """After a restart: a recorder still running is its own process, so the
    next ticks keep or stop it by pid and a restart mid-meeting does not cut
    it in two. One that stopped, or ran past MAX_RECORDING_S, is finished.
    One whose transcription the restart cut short is transcribed again (one
    with notes was in its agent turn: the Inbox offers that a retry)."""
    for item in inbox.items():
        if (item["source"] == "meeting" and item["status"] == "working" and not item["meta"].get("notes")
                and not item["meta"].get("cloud")):
            item["status"] = "recorded"
            inbox.save(item)
        if item["source"] == "meeting" and item["status"] == "recording":
            meta = item["meta"]
            if _alive(meta.get("pid", 0)) and time.time() - meta.get("started", 0) < MAX_RECORDING_S:
                continue
            _stop(meta.get("pid", 0))
            item["status"] = "recorded"
            inbox.save(item)


KEEP_DAYS = 30          # a recording's own folder is kept this long; its notes, and a coached one's report and audio, stay
_pruned_at = 0.0


def prune(now: float | None = None) -> int:
    """Remove recording folders older than KEEP_DAYS that nothing is working on."""
    now = now or time.time()
    busy = {i["meta"].get("dir") for i in inbox.items()
            if i["source"] == "meeting" and i["status"] in ("recording", "paused", "recorded", "working")}
    n = 0
    for folder in recordings_dir().glob("*") if recordings_dir().is_dir() else []:
        if folder.is_dir() and str(folder) not in busy and now - folder.stat().st_mtime > KEEP_DAYS * 86400:
            shutil.rmtree(folder, ignore_errors=True)
            n += 1
    return n
