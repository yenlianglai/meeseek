import { useCallback, useEffect, useRef, useState } from "react";
import { loadCommands } from "./lib/commands";
import {
  CONSOLE_USER,
  INBOX_USER,
  attachTurn,
  createSession,
  deleteSession,
  getAuthStatus,
  getSession,
  listSessions,
  resumeTurn,
  stopTurn,
  streamTurn,
} from "./lib/api";
import { clearTitle, sessionTitle, setTitle } from "./lib/titles";
import { useStableCallback } from "./lib/hooks";
import { markInboxOpened, openedInbox } from "./lib/inboxRail";
import { Chat } from "./chat/Chat";
import { IconChart, IconChat, IconInbox, IconLearning, IconMenu, IconMoon, IconSun, IconTrace, Logo } from "./ui/Icons";
import { useTheme } from "./lib/theme";
import { Inbox, useInboxFeed } from "./views/Inbox";
import { OutputProvider } from "./chat/OutputViewer";
import { Sidebar, sourceLabel } from "./views/Sidebar";
import { Trace } from "./views/Trace";
import { Usage } from "./views/Usage";
import { Context } from "./views/Context";
import { ConversationFolder, foldEvents } from "./lib/fold";
import { NavContext, useHashRoute } from "./lib/nav";
import { isSessionView, type Route, type View } from "./lib/route";
import type { Attachment, ChatMessage, ConsoleEvent, SessionRow, StreamEvent } from "./lib/types";


export default function App() {
  const [sessions, setSessions] = useState<SessionRow[] | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  // Who owns the conversation on screen. The rail lists the console's own
  // sessions and the Inbox's you have opened; another owner's (an MCP
  // caller's) is opened by a link, and is read-only unless it is the Inbox's.
  const [source, setSource] = useState(CONSOLE_USER);
  const [events, setEvents] = useState<ConsoleEvent[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [route, navigate] = useHashRoute();
  const view = route.view;
  // The last place visited in each view, so a tab returns where you left it.
  const lastIn = useRef<Partial<Record<View, Route>>>({});
  lastIn.current[route.view] = route;
  // Route-driven loads wait for boot, which loads the routed session itself.
  const booted = useRef(false);
  // What was last asked to load — compared with the route instead of what has
  // already landed, so a load in flight is not started a second time.
  const requested = useRef<{ owner: string; session?: string }>({ owner: CONSOLE_USER });
  const [streaming, setStreaming] = useState(false);
  const [railOpen, setRailOpen] = useState(false); // the overlay rail, on narrow screens
  // The side list folded away on wide screens, so the page is all content.
  const [railHidden, setRailHidden] = useState(() => {
    try { return localStorage.getItem("console.rail") === "hidden"; } catch { return false; }
  });
  const toggleRail = useCallback(() => {
    if (window.matchMedia("(max-width: 860px)").matches) { setRailOpen(true); return; }
    setRailHidden((h) => {
      try { localStorage.setItem("console.rail", h ? "shown" : "hidden"); } catch { /* storage off */ }
      return !h;
    });
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "b") { e.preventDefault(); toggleRail(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleRail]);
  const [banner, setBanner] = useState("");
  // Separate from `banner`: that one is transient and gets cleared on every
  // session open/create, which would wipe a credential warning the user still
  // needs to act on.
  const [authWarning, setAuthWarning] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  // Every loader writes shared state after an await, so a load the user has
  // moved on from must not paint. Aborting the SSE reader is not enough: the
  // abort runs the run's `finally`, which would re-list the old owner's
  // sessions into the rail. So each load takes a ticket, switching source or
  // opening a session issues a new one, and a stale ticket drops its result.
  const loadSeq = useRef(0);
  const newLoad = () => ++loadSeq.current;
  const isCurrent = (seq: number) => seq === loadSeq.current;

  const readOnly = source !== CONSOLE_USER;
  // The Inbox's sessions are not yours to create or delete, but you carry them on.
  const canReply = !readOnly || source === INBOX_USER;

  /** The rail's list: the sessions started from this console, and the Inbox's you have opened. */
  const refreshSessions = useCallback(async (seq = loadSeq.current) => {
    try {
      const opened = openedInbox();
      const [own, inbox] = await Promise.all([
        listSessions(CONSOLE_USER),
        // The Inbox's half is extra: failing it leaves the rail your own list.
        opened.size ? listSessions(INBOX_USER).catch(() => [] as SessionRow[]) : [],
      ]);
      // Each row names its owner, which opening it has to ask under.
      const list = [...own, ...inbox.filter((s) => opened.has(s.id))];
      list.sort((a, b) => b.updated - a.updated);
      // Still return the list to the caller — its own ticket check decides what
      // to do with it — but only paint when this load is still the current one.
      if (isCurrent(seq)) setSessions(list);
      return list;
    } catch (e) {
      if (isCurrent(seq)) {
        setBanner(`Cannot reach the agent server: ${e}. Is it running on :8787?`);
        setSessions([]);
      }
      return [];
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Failing to load costs the palette only; typing a command still runs it.
  useEffect(() => { void loadCommands(); }, []);

  // Credential state, on boot and every 5 min after: the ADC can go bad while
  // the server runs (a "Testing" OAuth client's refresh token expires weekly),
  // and the failure is otherwise invisible until a tool 403s mid-task.
  useEffect(() => {
    const check = async () => {
      try {
        const { auth } = await getAuthStatus();
        setAuthWarning(
          !auth || auth.ok
            ? ""
            : `Google credentials: ${auth.reason ?? "unavailable"} — ${auth.fix ?? "re-run setup"}.`,
        );
      } catch {
        // Server unreachable is already reported by refreshSessions.
      }
    };
    check();
    // A background tab need not know; it checks again when it is looked at.
    const id = setInterval(() => { if (!document.hidden) void check(); }, 5 * 60 * 1000);
    const onShow = () => { if (!document.hidden) void check(); };
    document.addEventListener("visibilitychange", onShow);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onShow);
    };
  }, []);

  // Boot: load the routed owner's sessions and open the routed session (or the
  // newest).
  useEffect(() => {
    void selectSource(isSessionView(route) ? route.owner ?? CONSOLE_USER : CONSOLE_USER,
      isSessionView(route) ? route.session : undefined, true)
      .finally(() => { booted.current = true; });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The URL says which session to show; load it when it changes. A session
  // view without one gets the session on screen (or the newest) filled in.
  useEffect(() => {
    if (!booted.current || !isSessionView(route)) return;
    const owner = route.owner ?? CONSOLE_USER;
    if (owner !== requested.current.owner) {
      void selectSource(owner, route.session);
    } else if (route.session && route.session !== requested.current.session) {
      void openSession(route.session, owner);
    } else if (!route.session) {
      const fill = activeId ?? sessions?.find((s) => s.user === owner)?.id;
      if (fill) navigate({ ...route, session: fill }, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route, source, activeId, sessions]);

  /** Where a session lives: Chat, or its Trace when that is what you are reading. */
  function sessionRoute(id: string, owner: string = source): Route {
    return { view: view === "trace" ? "trace" : "chat", session: id,
             owner: owner === CONSOLE_USER ? undefined : owner };
  }

  // `seq` is supplied when this open is part of a larger load (boot, a source
  // switch) so the whole sequence shares one ticket; a direct click issues its
  // own, which supersedes whatever that larger load still had in flight.
  async function openSession(id: string, user: string = source, seq: number = newLoad()) {
    requested.current = { owner: user, session: id };
    abortRef.current?.abort(); // detach from whatever was watched; its turn runs on
    setStreaming(false);
    setActiveId(id);
    setRailOpen(false);
    try {
      const s = await getSession(id, user);
      if (!isCurrent(seq)) return;
      // A turn is running: paint the session up to it, then attach — the
      // stream replays the running turn's events so far (except the person's
      // message, which the store already has) and carries on live. A turn so
      // young it has no id yet has no stored events to filter either.
      const live = (s.running && s.running_turn) || "";
      const shown = live ? s.events.filter((e) => e.turn !== live || e.kind === "said") : s.events;
      setEvents(shown);
      const folded = foldEvents(shown);
      setMessages(folded);
      // Opened from the Inbox: it joins the rail from now on.
      if (user === INBOX_USER && markInboxOpened(id)) void refreshSessions(seq);
      setBanner("");
      if (s.running) {
        const got = await runTurn(folded, new ConversationFolder(`t${Date.now()}-`),
          (signal) => attachTurn(id, signal));
        // The turn ended between the load and the attach (204, nothing came):
        // load again for its tail, which the filter above left out.
        if (got === 0 && isCurrent(seq)) await openSession(id, user, seq);
      }
    } catch (e) {
      if (isCurrent(seq)) setBanner(`Failed to load session: ${e}`);
    }
  }

  // Changing owner is a context switch: the conversation on screen belongs to
  // the old owner and a live stream may still be writing into it, so tear both
  // down exactly as openSession does (a detach only: the turn runs on). The
  // rail stays your own list; another owner's session opens only when a link
  // names it.
  async function selectSource(user: string, want?: string, force = false) {
    if (user === source && !force) return;
    requested.current = { owner: user, session: want };
    const seq = newLoad(); // supersedes the outgoing owner's in-flight loads
    abortRef.current?.abort();
    setStreaming(false);
    setSource(user);
    setActiveId(null);
    setEvents([]);
    setMessages([]);
    setBanner("");
    const list = await refreshSessions(seq);
    if (!isCurrent(seq)) return;
    const open = want ?? (user === CONSOLE_USER ? list.find((s) => s.user === CONSOLE_USER)?.id : undefined);
    if (open) await openSession(open, user, seq);
  }

  const renameSession = useCallback((id: string, title: string) => {
    setTitle(id, title);
    setSessions((prev) => (prev ? [...prev] : prev)); // force a re-render off the new title
  }, []);

  const removeSession = useStableCallback(async (id: string) => {
    try {
      await deleteSession(id);
    } catch (e) {
      setBanner(`Failed to delete session: ${e}`);
      return;
    }
    clearTitle(id);
    const remaining = (sessions ?? []).filter((s) => s.id !== id);
    setSessions(remaining);
    if (activeId === id) {
      abortRef.current?.abort();
      setStreaming(false);
      if (remaining.length) {
        navigate(sessionRoute(remaining[0].id, remaining[0].user), { replace: true });
      } else {
        setActiveId(null);
        setEvents([]);
        setMessages([]);
        navigate({ view: "chat" }, { replace: true });
      }
    }
    refreshSessions();
  });
  const selectFromRail = useStableCallback((id: string, owner: string) => navigate(sessionRoute(id, owner)));

  async function newSession() {
    // Same ticket discipline as openSession/selectSource: creating a session
    // replaces what the panes show, so an openSession still in flight must not
    // paint the old conversation over the new empty one when it lands.
    const seq = newLoad();
    abortRef.current?.abort();
    setStreaming(false);
    try {
      const s = await createSession(CONSOLE_USER);
      await refreshSessions(seq);
      if (!isCurrent(seq)) return;
      // The server keeps a session once it is spoken in; until then the rail holds it.
      setSessions((prev) => (prev?.some((x) => x.id === s.id) ? prev : [s, ...(prev ?? [])]));
      requested.current = { owner: CONSOLE_USER, session: s.id };
      setSource(CONSOLE_USER);
      setActiveId(s.id);
      setEvents([]);
      setMessages([]);
      navigate({ view: "chat", session: s.id });
      setRailOpen(false);
      setBanner("");
    } catch (e) {
      setBanner(`Failed to create session: ${e}`);
    }
  }

  async function send(text: string, attachments: Attachment[] = []) {
    // The composer is hidden under a read-only source, but a shortcut or a
    // retry can still reach send(); the turn must not land in someone else's
    // session.
    if (!activeId || !canReply) return;
    // Typing during a run interrupts it (human-in-the-loop redirect): cancel
    // the turn on the server — detaching alone would leave it running — then
    // the new message takes over the session. stopTurn returns only once the
    // turn is gone, so the new one cannot be refused as "already running".
    if (streaming) {
      abortRef.current?.abort();
      await stopTurn(activeId).catch(() => {}); // it may have just finished on its own
    }
    // A new message abandons a paused turn: it is no longer offered to resume.
    // Copied only where a flag changes: the rest keep their identity, so their
    // memoized rows are not re-rendered.
    const history = messages.map((m) => (m.streaming || m.interrupted ? { ...m, streaming: false, interrupted: false } : m));
    // Live folding: history messages stay; new events append. The stream does
    // not echo the person's message, so it is drawn — and traced — from here.
    const folder = new ConversationFolder(`t${Date.now()}-`);
    folder.said(text, attachments);
    const said: ConsoleEvent = { kind: "said", turn: "", at: Date.now() / 1000, by: source, text, answers: "",
                                 files: attachments.map((a) => ({ mime: a.mimeType, url: "" })) };
    await runTurn(history, folder, (signal) => streamTurn(activeId, text, attachments, signal, source), [said]);
  }

  /** Carry on a paused or failed turn from its last step, in the same message. */
  async function resume() {
    if (!activeId || !canReply || streaming) return;
    const last = messages[messages.length - 1];
    const folder = new ConversationFolder(`t${Date.now()}-`);
    folder.messages = [{ ...last, streaming: true, interrupted: false, error: undefined, notice: undefined }];
    await runTurn(messages.slice(0, -1), folder,
      (signal) => resumeTurn(activeId, signal, source));
  }

  async function runTurn(
    history: ChatMessage[],
    folder: ConversationFolder,
    open: (signal: AbortSignal) => AsyncGenerator<StreamEvent>,
    local: ConsoleEvent[] = [],
  ): Promise<number> {
    setBanner("");
    setStreaming(true);
    const abort = new AbortController();
    abortRef.current = abort;
    // The run holds the ticket current when it started (sending does not change
    // what is on screen). A source switch mid-run bumps the counter, and from
    // then on nothing this run produces may paint.
    const seq = loadSeq.current;
    const newEvents: ConsoleEvent[] = [...local];
    // The folder edits its messages in place, so each paint hands React a copy
    // of this turn's (the history keeps its identity), and paints at most once
    // a frame however fast tokens arrive. An aborted run paints nothing more:
    // stop() or the next run owns the screen from then on.
    let frame = 0;
    const paint = () => {
      frame = 0;
      if (abort.signal.aborted || !isCurrent(seq)) return;
      setMessages([...history, ...folder.messages.map((m) => ({ ...m, tools: m.tools.map((t) => ({ ...t })) }))]);
    };
    const flush = () => { cancelAnimationFrame(frame); paint(); };
    paint();
    try {
      for await (const event of open(abort.signal)) {
        if (event.kind !== "retry" && event.kind !== "error") newEvents.push(event);
        folder.push(event);
        if (!frame) frame = requestAnimationFrame(paint);
      }
      folder.finish();
      folder.markInterrupted();
      flush();
    } catch (e) {
      if (!abort.signal.aborted && isCurrent(seq)) {
        folder.finish();
        folder.markInterrupted(); // retries spent: the user can still resume by hand
        const last = folder.messages[folder.messages.length - 1];
        if (last && last.role === "agent") {
          last.error = String(e);
          last.notice = undefined;
        } else
          folder.messages.push({
            key: "err",
            role: "agent",
            author: "console",
            text: "",
            tools: [],
            error: String(e),
          });
        flush();
      }
    } finally {
      cancelAnimationFrame(frame);
      setStreaming(false);
      if (isCurrent(seq)) {
        setEvents((prev) => [...prev, ...newEvents]);
        refreshSessions(seq);
      }
    }
    // How many stream events arrived: an attach that caught nothing means the
    // turn had already ended, and the caller reloads instead.
    return newEvents.length - local.length;
  }

  /** Pause: cancel the turn on the server (a mere disconnect no longer would); it waits to be resumed. */
  function stop() {
    if (activeId) void stopTurn(activeId).catch(() => {});
    abortRef.current?.abort();
    setStreaming(false);
    setMessages((prev) => {
      const out = prev.map((m) => ({ ...m, streaming: false, notice: undefined }));
      const last = out[out.length - 1];
      if (last && last.role === "agent" && last.turn && !last.error) last.interrupted = true;
      return out;
    });
  }


  const inbox = useInboxFeed();
  const [theme, toggleTheme] = useTheme();
  const active = sessions?.find((s) => s.id === activeId);
  const sessionScoped = isSessionView(route);
  const ownerParam = source === CONSOLE_USER ? undefined : source;

  /** A top tab returns to where you last were in it. */
  function goTo(v: "chat" | "inbox" | "usage" | "context") {
    if (v === "chat") {
      const back = lastIn.current.trace ?? lastIn.current.chat;
      navigate(back && isSessionView(back) ? back : { view: "chat", session: activeId ?? undefined, owner: ownerParam });
    } else {
      navigate(lastIn.current[v] ?? { view: v });
    }
  }

  return (
    <NavContext.Provider value={navigate}>
    <OutputProvider>
    <div className="app">
    {/* One header for every view, so nothing in it moves when the view does:
        the brand holds the rail's width on the left, the view switch and the
        theme hold the right edge; what belongs to this view sits between. */}
    <header className="topbar">
      <span className="brand">
        {/* Home is the conversation: the logo goes back to Chat, where you left it. */}
        <button type="button" className="brand-home" onClick={() => goTo("chat")} title="Back to Chat"
                aria-label="meeseek — back to Chat">
          <Logo className="logo" />
          <span className="wordmark">meeseek<span>_</span></span>
        </button>
      </span>
      <button className="rail-toggle" onClick={toggleRail}
              style={{ visibility: sessionScoped ? "visible" : "hidden" }}
              aria-label={sessionScoped ? "Show or hide the session list" : "Show or hide the run list"}
              title="Show or hide the list (⌘B)">
        <IconMenu />
      </button>
      <span className="session-title" title={sessionScoped && active ? sessionTitle(active) : undefined}>
        {sessionScoped ? (active ? sessionTitle(active) : "No session") : ""}
      </span>
      {sessionScoped && activeId && (
        <button className="btn-ghost small" aria-pressed={view === "trace"}
                title={view === "trace" ? "Back to the conversation" : "Every event of this session"}
                onClick={() => navigate({ view: view === "trace" ? "chat" : "trace", session: activeId, owner: ownerParam })}>
          {view === "trace" ? "← Conversation" : <><IconTrace /> Trace</>}
        </button>
      )}
      <div className="seg" role="group" aria-label="View">
        <button aria-pressed={sessionScoped} onClick={() => goTo("chat")}>
          <IconChat /> Chat
        </button>
        <button aria-pressed={view === "inbox"} onClick={() => goTo("inbox")}>
          <IconInbox /> Inbox
          {inbox.waiting > 0 && <span className="badge" aria-label={`${inbox.waiting} waiting`}>{inbox.waiting}</span>}
        </button>
        <button aria-pressed={view === "usage"} onClick={() => goTo("usage")}>
          <IconChart /> Usage
        </button>
        <button aria-pressed={view === "context"} onClick={() => goTo("context")}>
          <IconLearning /> Context
        </button>
      </div>
      <button type="button" className="theme-toggle" onClick={toggleTheme}
              aria-label={theme === "dark" ? "Use the light theme" : "Use the dark theme"}
              title={theme === "dark" ? "Light theme" : "Dark theme"}>
        {theme === "dark" ? <IconSun /> : <IconMoon />}
      </button>
    </header>
    <div className={["shell", sessionScoped ? "" : "no-rail", railHidden ? "rail-hidden" : ""].join(" ").trim()}>
      {sessionScoped && (
        <Sidebar
          sessions={sessions}
          activeId={activeId}
          open={railOpen}
          onSelect={selectFromRail}
          onNew={newSession}
          onRename={renameSession}
          onDelete={removeSession}
        />
      )}
      {sessionScoped && railOpen && (
        <button className="scrim" aria-label="Close session list" onClick={() => setRailOpen(false)} />
      )}

      <main className="main">
        {authWarning && <div className="notice">{authWarning}</div>}
        {banner && sessionScoped && <div className="notice">{banner}</div>}

        {route.view === "chat" ? (
          <Chat
            messages={messages}
            streaming={streaming}
            hasSession={!!activeId}
            sessionId={activeId}
            readOnlyNote={
              !canReply
                ? `Read-only — this conversation came from ${sourceLabel(source)}.`
                : undefined
            }
            onSend={send}
            onStop={stop}
            onResume={canReply ? resume : undefined}
          />
        ) : route.view === "trace" ? (
          <Trace events={events} />
        ) : route.view === "inbox" ? (
          <Inbox itemId={route.item} feed={inbox} />
        ) : route.view === "usage" ? (
          <Usage days={route.days} />
        ) : route.view === "context" ? (
          <Context kind={route.kind} path={route.path} />
        ) : null}
      </main>
    </div>
    </div>
    </OutputProvider>
    </NavContext.Provider>
  );
}
