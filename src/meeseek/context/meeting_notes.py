"""Meeting notes: a meeting's transcript → structured notes in the knowledge base.

`notes_from_transcript` is how every recorded meeting becomes notes — the
laptop's recordings and Meet's own (runtime/meeting_recorder.py,
runtime/meet_cloud.py), transcribed once with who said what
(runtime/meeting_transcript.py). `process_meetings` is the manual backfill:
recordings in the meetings folder (the Google pack's meeting_recordings) not
processed before, downloaded, their sound taken out, transcribed and turned
into notes the same way.
Notes and transcripts land in knowledge_base/sources/meetings/.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from meeseek.config import cfg
from meeseek.config.paths import home
from meeseek.context import prompts
from meeseek.runtime import packs
from meeseek.runtime.oneshot import ask

_log = logging.getLogger(__name__)


def meetings_dir() -> Path:
    return home().sources / "meetings"


_MAX_PARALLEL = 5

_SUPPORTED_MIMES = {
    "audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav", "audio/webm",
    "audio/x-m4a", "audio/aac", "audio/flac",
    "video/mp4", "video/webm", "video/x-matroska", "video/quicktime",
}


def _load_history() -> dict:
    if (home().data / "meeting_sync_history.json").exists():
        return json.loads((home().data / "meeting_sync_history.json").read_text(encoding="utf-8"))
    return {}


def _save_history(history: dict):
    (home().data / "meeting_sync_history.json").write_text(
        json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _download_recording(file_id: str, file_name: str) -> Path:
    """Download a recording to the local cache (through the pack). Skips if already cached."""
    (home().data / "meeting_downloads").mkdir(parents=True, exist_ok=True)
    ext = Path(file_name).suffix or ".mp4"
    safe_name = f"meeting_{file_id}{ext}"
    cache_path = (home().data / "meeting_downloads") / safe_name

    if cache_path.exists() and cache_path.stat().st_size > 0:
        _log.info("Using cached download: %s (%.0f MB)", file_name[:30], cache_path.stat().st_size / (1024 * 1024))
        return cache_path

    packs.call_json("recording_fetch", {"recording_id": file_id, "dest": str(cache_path)})

    _log.info("Downloaded %s (%.0f MB)", file_name[:30], cache_path.stat().st_size / (1024 * 1024))
    return cache_path


def _load_team_context() -> str:
    """The team roster (notes/pinned/team_members.md) for speaker
    identification; empty when absent."""

    team_file = home().notes / "pinned" / "team_members.md"
    if team_file.exists():
        return team_file.read_text(encoding="utf-8").strip()
    return ""


def notes_from_transcript(transcript: str, name: str, *, date: str, transcript_md: str = "") -> Path:
    """Structured notes from a meeting's transcript (one turn a line); writes
    <name>.md and <name>_transcript.md under MEETINGS_DIR and returns the notes'
    path. `date` is the meeting's (YYYY-MM-DD); `transcript_md` the transcript
    as a person reads it, kept beside the notes."""
    from meeseek.context import ontology

    team_context = _load_team_context()
    # Who reports on what, from past meetings: how a 與會者 line is put to a name.
    who = ontology.summary()
    if who:
        team_context = f"{team_context}\n\n從過去會議推斷的角色與關係：\n{who[:3000]}"
    notes = ask(prompts.text("internal/meeting_extract", team_context=team_context,
                             today=date, source_name=name, transcript=transcript[:80000]),
                model=cfg.models.workflow, temperature=0.2, max_output_tokens=8000)
    if not notes:
        raise ValueError(f"{name}: no notes came back")

    meetings_dir().mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name.rsplit(".", 1)[0])
    output_path = meetings_dir() / f"{safe_name}.md"
    output_path.write_text(notes, encoding="utf-8")
    (meetings_dir() / f"{safe_name}_transcript.md").write_text(transcript_md or transcript, encoding="utf-8")
    return output_path


def _process_single_recording(rec: dict) -> tuple[str, str | None]:
    """Process a single recording end-to-end. Returns (status_msg, file_id or None)."""
    from meeseek.runtime.meeting_transcript import MIN_CHARS, plain, render, said, transcript

    name = rec["name"]
    file_id = rec["id"]
    folder = (home().data / "meeting_downloads") / f"rec_{file_id}"
    try:
        local_path = _download_recording(file_id, name)
        folder.mkdir(parents=True, exist_ok=True)
        # The sound alone, as the meeting's one track (video is 8x the tokens).
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(local_path), "-vn", "-ac", "1",
                        "-ar", "16000", "-c:a", "aac", "-b:a", "48k", str(folder / "app.m4a")],
                       check=True, capture_output=True, timeout=1800)
        local_path.unlink(missing_ok=True)
        lines = transcript(folder, roster=_load_team_context())
        if said(lines) < MIN_CHARS:
            raise ValueError(f"{name}: almost nothing is said in it")
        date = (rec.get("createdTime") or rec.get("modifiedTime") or datetime.now().isoformat())[:10]
        output_path = notes_from_transcript(plain(lines), name, date=date, transcript_md=render(lines, name))
        try:
            from meeseek.context import ontology

            ontology.learn(plain(lines), output_path.read_text(encoding="utf-8"), title=name, date=date)
        except Exception:  # noqa: BLE001 — the notes are made; who is who can wait
            _log.exception("[ontology] %s", name)
        return (f"✅ {name} → {output_path.name}", file_id)

    except ValueError as e:
        return (f"⚠️ {e}", None)
    except Exception as e:
        _log.exception("Failed to process %s", name)
        return (f"❌ {name}: {e}", None)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def process_meetings() -> str:
    """Start processing new meeting recordings from the meetings folder in the background.

    Downloads recordings, extracts audio, splits into chunks, transcribes with Gemini,
    and extracts structured notes. Saves to knowledge_base/sources/meetings/.

    Returns immediately with status. Call `check_meetings_status()` for progress.

    Use this when the user says "process meetings", "/meetings", or similar.
    """
    if _background_task.is_running():
        return f"Meeting processing already in progress. {_background_task.status()}"

    try:
        listed = packs.call_json("meeting_recordings")
    except Exception as e:  # noqa: BLE001 — no Google pack, or it cannot reach Drive
        return f"Could not list the meetings folder: {e}"
    if "error" in listed:
        return listed["error"]

    meetings_dir().mkdir(parents=True, exist_ok=True)
    history = _load_history()

    all_files = listed.get("files") or []
    recordings = [f for f in all_files if f.get("mimeType", "") in _SUPPORTED_MIMES]

    if not recordings:
        return "No recordings found in the meetings folder."

    to_process = []
    for rec in recordings:
        record = history.get(rec["id"], {})
        if record.get("modifiedTime") != rec["modifiedTime"]:
            to_process.append(rec)

    if not to_process:
        return f"All {len(recordings)} recordings already processed."

    _background_task.start(to_process)
    return f"Started processing {len(to_process)} recordings in background. Use `check_meetings_status()` to check progress."


def check_meetings_status() -> str:
    """Check the progress of background meeting processing.

    Returns current status, progress, and results when complete.
    """
    return _background_task.status()


# ---------------------------------------------------------------------------
# Background Task Manager
# ---------------------------------------------------------------------------


class _MeetingBackgroundTask:
    """Manages background meeting processing with status tracking."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._progress: list[str] = []
        self._total: int = 0
        self._done: int = 0
        self._running = False
        self._result: str = ""

    def is_running(self) -> bool:
        return self._running

    def start(self, to_process: list[dict]):
        self._progress = []
        self._total = len(to_process)
        self._done = 0
        self._running = True
        self._result = ""
        self._thread = threading.Thread(
            target=self._run, args=(to_process,), daemon=True
        )
        self._thread.start()

    def status(self) -> str:
        if not self._running and not self._result:
            return "No meeting processing task running."
        if self._running:
            progress_pct = (self._done / self._total * 100) if self._total else 0
            lines = [f"Processing: {self._done}/{self._total} ({progress_pct:.0f}%)"]
            if self._progress:
                lines.extend(self._progress[-3:])
            return "\n".join(lines)
        return self._result

    def _run(self, to_process: list[dict]):
        try:
            history = _load_history()
            results: list[str] = []

            with ThreadPoolExecutor(max_workers=_MAX_PARALLEL) as pool:
                futures = {
                    pool.submit(_process_single_recording, rec): rec
                    for rec in to_process
                }
                for future in as_completed(futures):
                    status_msg, file_id = future.result()
                    results.append(status_msg)
                    self._done += 1
                    self._progress.append(status_msg)

                    if file_id:
                        rec = futures[future]
                        history[file_id] = {
                            "name": rec["name"],
                            "modifiedTime": rec["modifiedTime"],
                            "local_path": str(meetings_dir() / "..."),
                            "processed_at": datetime.now().isoformat(),
                        }

            _save_history(history)
            self._result = f"Done! Processed {len(results)}/{self._total} recordings:\n" + "\n".join(results)
        except Exception as e:
            self._result = f"Error during processing: {e}"
            _log.exception("Background meeting processing failed")
        finally:
            self._running = False


_background_task = _MeetingBackgroundTask()

