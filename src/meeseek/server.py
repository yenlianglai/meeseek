"""The meeseek server: the web console and its session and turn API
(surfaces/console.py), the console's own endpoints, the MCP server, and periodic jobs (JOB_SCHEDULE).

Usage:
    meeseek serve                       # or, from a checkout: uv run python server.py
    meeseek serve --port 8080
    meeseek serve --no-scheduler        # disable periodic jobs
    meeseek serve --host 0.0.0.0        # reachable from other machines (the MCP surface has no auth)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException

log = logging.getLogger(__name__)

from meeseek.config import OUTPUTS_URL_PREFIX, PROJECT_ROOT, cfg  # noqa: E402
from meeseek.config.paths import home  # noqa: E402

# How an agent-written page is served (see _pages_run_sandboxed): scripts and
# CDN libraries run, but in an opaque origin that can reach no URL.
PAGE_CSP = ("sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads; "
            "connect-src 'none'; form-action 'none'; base-uri 'none'")
from meeseek.runtime.meet_cloud import POLL_S as MEET_CLOUD_POLL_S  # noqa: E402
from meeseek.runtime.meeting_recorder import TICK_SECONDS  # noqa: E402

# ---------------------------------------------------------------------------
# Periodic jobs (sync ones run in APScheduler's thread pool)
# ---------------------------------------------------------------------------


def _drive_sync():
    from meeseek.context.sync import sync

    try:
        result = sync()
    except RuntimeError as e:   # the Google pack's answer, when Drive refused
        if "insufficient" in str(e).lower() and "scope" in str(e).lower():
            # ADC was created without Drive scopes — one clear line instead of
            # a full traceback every 30 minutes until the user re-logs in.
            log.warning(
                "[drive_sync] skipped: ADC lacks Drive scopes. Re-login with the "
                "command in README → Setup → 4. Google login to enable Drive sync."
            )
            return None
        raise
    log.info("[drive_sync] %s", result)
    return result


def _rule_review():
    from meeseek.runtime.rule_review import spawn

    log.info("[rule_review] %s", "started" if spawn() else "already running")


async def _triggers():
    from meeseek.runtime.triggers import poll

    out = await poll()
    if out["filed"]:
        log.info("[triggers] filed %d item(s)", len(out["filed"]))


async def _meeting_recorder():
    from meeseek.runtime import meeting_recorder as recorder
    from meeseek.runtime.meeting_recorder import tick
    from meeseek.app import app

    status = app().trigger_status

    try:
        await tick()
        # Meetings found by the calendar alone still record; the reason the tabs
        # could not be read is shown, so it gets fixed.
        status["recorder"] = {"ok": True, "error": recorder.fallback_reason, "at": time.time(), "filed": 0}
    except Exception as e:  # noqa: BLE001 — Chrome not scriptable yet, a permission missing
        log.warning("[meeting_recorder] %s", e)
        status["recorder"] = {"ok": False, "error": str(e), "at": time.time(), "filed": 0}


async def _learn_about_you():
    """What the day's conversations said about how you want things (context/personal.py)."""
    from meeseek.context import personal

    try:
        n = await asyncio.to_thread(personal.learn_from_chat, 1)
        log.info("[personal] %d fact(s) from today's conversations", n)
    except Exception as e:  # noqa: BLE001 — tomorrow's run reads its own day
        log.warning("[personal] %s", e)
    await _tidy_context()


async def _tidy_context():
    """What has run out leaves the learned stores (context/lifecycle.py), on
    days nothing new is learned too: the team graph, the facts about you, the
    remembered facts past their window, the turn records past eight weeks and
    the conversations past `learning.session_days`; and the files that pile
    up, past their own windows."""
    from meeseek import engine
    from meeseek.config import cfg
    from meeseek.context import memory, ontology, personal
    from meeseek.runtime import (cases, claude_agent, daily_review, improve, meeting_coach, page_shot, reward,
                                 sessions)

    for name, tidy in (("ontology", ontology.tidy), ("personal", personal.tidy), ("facts", memory.tidy_facts),
                       # what meeseek learns from its own use: old turns folded into weekly lines, and
                       # conversations past the window the past-sessions tools read
                       ("turns", reward.compact), ("sessions", lambda: sessions.prune(cfg.learning.session_days)),
                       ("conversations", lambda: engine.store().prune(time.time() - cfg.learning.session_days * 86400)),
                       # files that pile up: Claude's scratch folders, page pictures, coached meetings' audio
                       ("claude scratch", claude_agent.prune_scratch), ("page shots", page_shot.prune),
                       ("review worktrees", daily_review.prune_worktrees),
                       ("meeting audio", meeting_coach.prune_audio),
                       # the case library: graded turns in, stale ones out (runtime/cases.py)
                       ("cases", lambda: cases.mint(2)), ("cases retired", cases.retire),
                       ("runs", cases.prune_runs),
                       # an approved change that made its skill worse: a rollback proposed (runtime/improve.py)
                       ("rollbacks", improve.monitor), ("improve", improve.tidy)):
        try:
            if n := await asyncio.to_thread(tidy):
                log.info("[tidy] %s: %d retired", name, n)
        except Exception as e:  # noqa: BLE001 — one store's failure leaves the others tidied
            log.warning("[tidy] %s: %s", name, e)


async def _announce_proposals():
    """A proposal from the weekly improvement run, on your phone (runtime/improve.py)."""
    from meeseek.runtime import messengers

    try:
        await messengers.announce_proposals()
    except Exception as e:  # noqa: BLE001 — the Learning page shows it all the same
        log.warning("[improve] announcing proposals: %s", e)


async def _inbox_digest():
    """What waits unseen in the Inbox, summed up on the phone (runtime/messengers)."""
    from meeseek.runtime import messengers

    try:
        if await messengers.inbox_digest():
            log.info("[digest] sent")
    except Exception as e:  # noqa: BLE001 — the next cron tries again
        log.warning("[digest] %s", e)


async def _daily_review():
    """The day's errors, failed turns and corrections, handed to Claude Code for
    causes and ranked fixes: one Inbox item (runtime/daily_review.py)."""
    from meeseek.runtime import daily_review

    try:
        item = await asyncio.to_thread(daily_review.run)
        log.info("[review] %s", f"filed {item['id']}" if item else "nothing filed")
    except Exception as e:  # noqa: BLE001 — tomorrow's run reads its own day
        log.warning("[review] %s", e)


async def _drift_report():
    """The knowledge base's design and engineering-management documents against
    the main repo's docs/ and code: one Inbox item when they disagree
    (runtime/drift_report.py)."""
    from meeseek.runtime import drift_report

    try:
        record = await asyncio.to_thread(drift_report.run)
        log.info("[drift] %s", f"Claude compares ({record['id']})" if record else "nothing started")
    except Exception as e:  # noqa: BLE001 — next week's run compares again
        log.warning("[drift] %s", e)


async def _wakes():
    """Run the wakes that have come due (runtime/wakes.py): filed retries and
    Claude follow-ups — the records a restart cannot lose."""
    from meeseek.runtime import wakes

    try:
        if n := await wakes.poll():
            log.info("[wakes] ran %d", n)
    except Exception as e:  # noqa: BLE001 — the next poll tries again
        log.warning("[wakes] %s", e)


async def _meet_cloud():
    from meeseek.runtime import meet_cloud

    try:
        if n := await meet_cloud.poll():
            log.info("[meet_cloud] fetching %d cloud recording(s)", n)
    except Exception as e:  # noqa: BLE001 — the next poll tries again
        log.warning("[meet_cloud] %s", e)


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler()

JOB_SCHEDULE = {
    "drive_sync": {"func": _drive_sync, "cron": "*/30 * * * *"},
    "rule_review": {"func": _rule_review, "cron": "0 2 * * mon"},
    "learn_about_you": {"func": _learn_about_you, "cron": "30 3 * * *"},   # and the nightly tidy
    "announce_proposals": {"func": _announce_proposals, "cron": "*/15 * * * *"},
    "wakes": {"func": _wakes, "seconds": 30},
    **({"inbox_digest": {"func": _inbox_digest, "cron": cfg.triggers.digest_cron}}
       if cfg.triggers.digest_cron and cfg.messengers.telegram else {}),
    **({"daily_review": {"func": _daily_review, "cron": cfg.triggers.review_cron}}
       if cfg.triggers.review_cron and cfg.claude.enabled else {}),
    **({"drift_report": {"func": _drift_report, "cron": cfg.triggers.drift_cron}}
       if cfg.triggers.drift_cron and cfg.claude.enabled else {}),
    **({"triggers": {"func": _triggers, "cron": "* * * * *"}}
       if cfg.triggers.sources
       else {}),
    **({"meeting_recorder": {"func": _meeting_recorder, "seconds": TICK_SECONDS}}
       if cfg.triggers.record_meetings and sys.platform == "darwin" else {}),
    # Meet's own recordings of meetings the laptop did not record: yours, and the
    # coached ones. The one automatic way a cloud recording becomes notes.
    **({"meet_cloud": {"func": _meet_cloud, "seconds": MEET_CLOUD_POLL_S}}
       if any(s.pack == "google" for s in cfg.mcp_servers)
       and (cfg.workflows.meeting_notes or cfg.triggers.coach_meetings) else {}),
}


def _register_jobs():
    for job_id, job in JOB_SCHEDULE.items():
        scheduler.add_job(
            job["func"],
            IntervalTrigger(seconds=job["seconds"]) if "seconds" in job else CronTrigger.from_crontab(job["cron"]),
            id=job_id,
            replace_existing=True,
            # A sleeping laptop misses its slots (the 03:30 job, Monday's
            # 02:00): run each missed job once on waking, however late,
            # rather than dropping it — and once, not once per missed slot.
            misfire_grace_time=None,
            coalesce=True,
        )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


def create_app(enable_scheduler: bool = True) -> FastAPI:
    """The web server: the console's API, the MCP surface, the outputs, and
    (with `enable_scheduler`) the periodic jobs."""
    from meeseek.app import build_app

    build_app().install().home.ensure()

    async def _warmup_mcp():
        """Pre-connect MCP toolsets so the first user request doesn't pay the
        cold start (uvx download + stdio handshake can take 1-2 min, during
        which every turn appears dead)."""
        from meeseek import engine

        t0 = time.monotonic()
        built = await engine.with_mcp(engine.agents())
        log.info("[warmup] MCP tools ready in %.1fs (%d agents)", time.monotonic() - t0, len(built))
        if mcp_server is not None:
            from meeseek.runtime.mcp_server import add_pack_tools

            added = await add_pack_tools(mcp_server)
            log.info("[warmup] MCP surface: %d pack tool(s) offered", len(added))

    # Built before the app so its session manager can be started in the
    # lifespan below — a mounted sub-app never gets a lifespan of its own.
    try:
        from meeseek.runtime.mcp_server import build_mcp_server

        mcp_server = build_mcp_server()
    except Exception:
        log.warning("MCP server unavailable — /mcp-server disabled", exc_info=True)
        mcp_server = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from meeseek.runtime import inbox, meeting_recorder

        from meeseek.runtime import claude_agent, daily_review, wakes

        daily_review.install()    # warnings kept on disk for the review, and its Claude sessions heard
        from meeseek.runtime import drift_report

        drift_report.install()    # the drift report's Claude session, heard when it ends
        from meeseek.runtime import workstreams

        workstreams.install()     # Claude sessions about a piece of work fold into its workstream

        # Wakes first: a retry or follow-up the dead process claimed goes back
        # to pending, claude_agent.recover fires the wakes of interrupted
        # tasks, and inbox.recover leaves items whose retry wake survived.
        wakes.recover()
        meeting_recorder.recover()
        claude_agent.recover()
        inbox.recover()
        from meeseek.runtime import messengers

        asyncio.create_task(messengers.start())   # connecting can take seconds; the server need not wait
        if enable_scheduler:
            _register_jobs()
            scheduler.start()
            log.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))
        warmup = asyncio.create_task(_warmup_mcp())
        if mcp_server is not None:
            async with mcp_server.session_manager.run():
                yield
        else:
            yield
        warmup.cancel()
        await messengers.stop()
        if enable_scheduler:
            scheduler.shutdown(wait=False)

    # The console's sessions and turns are served from the event store
    # (surfaces/console.py); everything else here is meeseek's own API.
    from fastapi.middleware.cors import CORSMiddleware

    from meeseek.surfaces import console

    web = FastAPI(lifespan=lifespan)
    web.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    console.install(web)

    # --- Custom endpoints ---

    @web.get("/slash-commands")
    def slash_commands():
        """The commands the dispatcher answers to, for the UI's palette."""
        from meeseek.runtime.slash_commands import slash_command_list
        return slash_command_list()

    # What the team produced, read-only over HTTP, so the console can embed
    # rendered diagrams and generated images. StaticFiles serves GET/HEAD only
    # and refuses to escape the root.
    from fastapi.staticfiles import StaticFiles as _StaticFiles

    web.mount(OUTPUTS_URL_PREFIX, _StaticFiles(directory=home().outputs), name="outputs")
    log.info("Outputs served read-only at %s (from %s)", OUTPUTS_URL_PREFIX, home().outputs)

    @web.middleware("http")
    async def _pages_run_sandboxed(request, call_next):
        """A page an agent wrote (HTML, or an SVG, which can script too) runs in
        a sandbox wherever it is opened — in the console's frame or a tab of its
        own: an opaque origin, no network, no forms. Without it a page's script
        would be the console, able to call its API (approve a held write)."""
        response = await call_next(request)
        path = request.url.path.lower()
        if path.startswith(OUTPUTS_URL_PREFIX) and path.endswith((".html", ".htm", ".svg")):
            response.headers["Content-Security-Policy"] = PAGE_CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    # Custom web console (frontend/) — served at /ui from the same port.
    # Build with `npm run build --prefix frontend` (scripts/serve.sh does this).
    console_dist = PROJECT_ROOT / "frontend" / "dist"
    if console_dist.is_dir():
        from fastapi.staticfiles import StaticFiles

        web.mount("/ui", StaticFiles(directory=console_dist, html=True), name="console")
        log.info("Web console mounted at /ui (from %s)", console_dist)
    else:
        log.warning("frontend/dist not found — /ui disabled. Run: npm run build --prefix frontend")

    # MCP server — makes this team callable BY an MCP host (Claude Code /
    # Desktop). Mounted here rather than launched over stdio so a call reuses
    # this warm process instead of paying skill + toolset startup every time.
    if mcp_server is not None:
        web.mount("/mcp-server", mcp_server.streamable_http_app())
        log.info("MCP server mounted at /mcp-server/mcp")

    @web.get("/usage_api/summary")
    def usage_summary(days: int = 30):
        """LLM usage across every call path (agent turns, direct calls, Claude Code)."""
        from meeseek.runtime.usage import summary

        return summary(days=max(1, min(days, 365)))

    # What you said about a turn (runtime/feedback.py): 👍/👎 with a note, a reply copied.
    # Only these two come in over HTTP; the Inbox records its own decisions.
    @web.post("/feedback_api/turn")
    def feedback_turn(body: dict):
        from meeseek.runtime import feedback

        inv, rating = str(body.get("turn", "")), body.get("rating")
        if not inv or rating not in (1, -1):
            raise HTTPException(status_code=400, detail="turn and rating (1 or -1) are required")
        feedback.record(inv, str(body.get("sessionId", "")), "thumb", value=rating,
                        note=str(body.get("note", ""))[:1000])
        return {"ok": True}

    @web.post("/feedback_api/copy")
    def feedback_copy(body: dict):
        from meeseek.runtime import feedback

        if not body.get("turn"):
            raise HTTPException(status_code=400, detail="turn is required")
        feedback.record(str(body["turn"]), str(body.get("sessionId", "")), "copy",
                        what=str(body.get("what", "answer"))[:20])
        return {"ok": True}

    # The weekly improvement run (runtime/improve.py): its proposals wait for you.
    @web.get("/learning_api/proposals")
    def learning_proposals():
        from meeseek.runtime import improve

        return list(reversed(improve.proposals()))[:30]

    @web.post("/learning_api/proposals/{pid}/{verdict}")
    def learning_decide(pid: str, verdict: str, body: dict | None = None):
        from meeseek.runtime import improve

        if verdict not in ("approve", "reject"):
            raise HTTPException(status_code=400, detail="approve or reject")
        try:
            return improve.decide(pid, verdict == "approve", note=str((body or {}).get("note", "")))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @web.get("/learning_api/cases")
    def learning_cases():
        from meeseek.runtime import cases

        keep = ("id", "skill", "kind", "status", "seen", "request", "reference", "label", "last_seen", "results")
        return [{k: c.get(k) for k in keep} for s in cases.skills() for c in cases.load(s)]

    @web.post("/learning_api/cases/{skill}/{cid}/{action}")
    def learning_case(skill: str, cid: str, action: str):
        from meeseek.runtime import cases

        case = next((c for c in cases.load(skill) if c["id"] == cid), None)
        if case is None or action not in ("pin", "retire"):
            raise HTTPException(status_code=404, detail="no such case or action")
        if action == "retire":
            cases.drop(case)
        else:
            case["confirmed"], case["status"] = True, "confirmed"
            cases.save(case)
        return {"ok": True}

    @web.get("/learning_api/runs")
    def learning_runs():
        from meeseek.runtime import cases

        return list(reversed(cases.runs()))[:30]

    @web.get("/learning_api/runs/{a}/diff/{b}")
    def learning_run_diff(a: str, b: str):
        from meeseek.runtime import cases

        return cases.diff(a, b)

    @web.get("/learning_api/scoreboard")
    def learning_scoreboard(days: int = 28, by: str = "skill"):
        from meeseek.runtime import reward

        return {"rows": reward.scoreboard(days=max(1, min(days, 180)), by="bundle" if by == "bundle" else "skill"),
                "judge_trusted": reward.judge_trusted()}

    # Learning page — learned rules and facts, and what a person decides about them.
    from meeseek.runtime import learning_page as _learning

    @web.get("/learning_api/rules")
    def learning_rules():
        return _learning.rules()

    @web.post("/learning_api/rules/{skill}/{rid}/graduate")
    def learning_graduate(skill: str, rid: str):
        try:
            return {"commit": _learning.graduate_rule(skill, rid)}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @web.delete("/learning_api/rules/{skill}/{rid}")
    def learning_forget_rule(skill: str, rid: str):
        try:
            _learning.forget_rule(skill, rid)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"ok": True}

    @web.get("/learning_api/review")
    def learning_review():
        return _learning.review()

    @web.post("/learning_api/review/run")
    def learning_review_run():
        from meeseek.runtime.rule_review import spawn

        return {"started": spawn()}

    @web.post("/learning_api/archive/{skill}/{rid}/restore")
    def learning_restore(skill: str, rid: str):
        try:
            _learning.restore_rule(skill, rid)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True}

    @web.get("/learning_api/facts")
    def learning_facts():
        return _learning.facts()

    @web.post("/learning_api/facts/{action}")
    def learning_fact_action(action: str, body: dict):
        act = {"pin": lambda b: _learning.pin_fact(b["line"]),
               "forget": lambda b: _learning.forget_fact(b["line"])}.get(action)
        if act is None:
            raise HTTPException(status_code=404, detail="unknown action")
        try:
            return {"ok": True, "result": act(body)}
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @web.get("/learning_api/skills")
    def learning_skills():
        return _learning.skills()

    @web.post("/learning_api/rules")
    def learning_add_rule(body: dict):
        try:
            return {"result": _learning.add_rule(body["skill"], body["text"], body.get("reason", ""))}
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @web.put("/learning_api/rules/{skill}/{rid}")
    def learning_edit_rule(skill: str, rid: str, body: dict):
        try:
            return {"id": _learning.edit_rule(skill, rid, body["text"], body.get("reason", ""))}
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    # Context files — pinned and other notes, meeting notes, agent outputs.
    from meeseek.runtime import context_store as _ctx

    def _ctx_call(fn, *args):
        try:
            return fn(*args)
        except _ctx.Conflict as e:
            raise HTTPException(status_code=409, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=f"no such file: {e}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @web.get("/context_api/{kind}")
    def context_list(kind: str):
        return _ctx_call(_ctx.list_files, kind)

    @web.get("/context_api/{kind}/file")
    def context_read(kind: str, path: str):
        return _ctx_call(_ctx.read, kind, path)

    @web.put("/context_api/{kind}/file")
    def context_write(kind: str, body: dict):
        if "path" not in body or "text" not in body:
            raise HTTPException(status_code=400, detail="path and text are required")
        return _ctx_call(_ctx.write, kind, body["path"], body["text"], body.get("etag"))

    @web.delete("/context_api/{kind}/file")
    def context_delete(kind: str, path: str, etag: str):
        _ctx_call(_ctx.delete, kind, path, etag)
        return {"ok": True}

    # Inbox — what the triggers filed, and your yes or no on what the agent wants to do.
    from meeseek.runtime import inbox as _inbox

    def _item(item_id: str) -> dict:
        item = _inbox.get(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="no such item")
        return item

    @web.get("/inbox_api/items")
    def inbox_items():
        return _inbox.items()

    @web.get("/inbox_api/status")
    def inbox_status():
        from meeseek.app import app
        from meeseek.runtime import messengers, triggers

        recorder = ["recorder"] if cfg.triggers.record_meetings and sys.platform == "darwin" else []
        return {"enabled": triggers.enabled() + recorder + messengers.enabled(),
                "sources": {**app().trigger_status, **app().channel_status}}

    @web.post("/inbox_api/poll")
    async def inbox_poll():
        from meeseek.runtime import triggers

        return await triggers.poll()

    @web.post("/inbox_api/items/{item_id}/seen")
    def inbox_seen(item_id: str):
        _item(item_id)
        _inbox.mark_seen(item_id)
        return {"ok": True}

    @web.post("/inbox_api/items/{item_id}/dismiss")
    def inbox_dismiss(item_id: str, body: dict | None = None):
        _item(item_id)
        _inbox.dismiss(item_id, reason=str((body or {}).get("reason", ""))[:60])
        return {"ok": True}

    @web.post("/inbox_api/items/{item_id}/run")
    async def inbox_run(item_id: str):
        """Work on an item now: a retry, or one triage left as fyi."""
        from meeseek.runtime.triggers import message_for

        item = _item(item_id)
        if item["status"] == "working":
            raise HTTPException(status_code=409, detail="already working")
        if item["source"] == "meeting" and not item["message"]:
            item["status"] = "recorded"  # the recorder's next tick transcribes it
            _inbox.save(item)
            return {"ok": True}
        _inbox.start(item_id, item["message"] or message_for(item))
        return {"ok": True}

    @web.post("/inbox_api/items/{item_id}/actions/{action_id}/{verdict}")
    async def inbox_action(item_id: str, action_id: str, verdict: str, body: dict | None = None):
        _item(item_id)
        body = body or {}
        try:
            if verdict == "approve":
                message = _inbox.approve(item_id, action_id, body.get("args"))
            elif verdict == "reject":
                message = _inbox.reject(item_id, action_id, body.get("note", ""))
                if not body.get("tell_agent", True):
                    return {"ok": True}
            else:
                raise HTTPException(status_code=404, detail="unknown verdict")
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        _inbox.start(item_id, message)
        return {"ok": True}

    # Claude Code sessions on this machine (runtime/claude_agent.py), for the
    # composer's picker: which one a task goes to. meeseek does the handing over.
    @web.get("/claude_api/sessions")
    def claude_sessions_list(query: str = "", limit: int = 50):
        from meeseek.runtime import claude_agent

        return claude_agent.sessions(query, limit=min(max(limit, 1), 200))

    @web.get("/auth_api/status")
    def auth_status():
        # The Google pack's check is TTL-cached and rebuilds + refreshes the ADC
        # on each miss, so polling this doubles as a credential liveness probe —
        # the console raises a banner when the ADC loses its Workspace scopes or
        # its refresh token expires, instead of the user hitting a 403 mid-task.
        from meeseek.runtime import packs

        try:
            return {"status": "ok", "auth": packs.call_json("auth_status")}
        except LookupError:
            return {"status": "ok", "auth": {"ok": True, "unverified": "no Google pack is running"}}
        except Exception as e:  # noqa: BLE001 — a status probe answers, it does not fail
            return {"status": "ok", "auth": {"ok": True, "unverified": f"{type(e).__name__}: {e}"[:300]}}

    return web


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def __getattr__(name: str):
    """`app`, built on first use, for the uvicorn import string "meeseek.server:app"."""
    if name == "app":
        globals()["app"] = instance = create_app()
        return instance
    raise AttributeError(name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meeseek serve",
                                     description="meeseek: the console, the MCP surface and the scheduler")
    parser.add_argument("--host", default="127.0.0.1",
                        help="127.0.0.1 (default): this machine only. The MCP surface has no auth.")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-scheduler", action="store_true", help="Disable periodic jobs")
    parser.add_argument("--reload", action="store_true", help="Enable hot reload (dev)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # httpx logs each request's URL at INFO, and Telegram's API puts the bot
    # token in the URL: the log would carry the secret.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.reload:
        uvicorn.run(
            "meeseek.server:app",
            host=args.host,
            port=args.port,
            reload=True,
        )
    else:
        instance = create_app(
            enable_scheduler=not args.no_scheduler,
        )
        uvicorn.run(
            instance,
            host=args.host,
            port=args.port,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
