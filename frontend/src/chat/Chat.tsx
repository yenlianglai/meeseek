import { memo, useEffect, useMemo, useRef, useState } from "react";
import { useStableCallback } from "../lib/hooks";
import { ClaudeTargetChip, ClaudeTargetPicker, directive, splitDirective, useClaudeTarget } from "./ClaudeTarget";
import { ACCEPT_ATTR, attachmentBytes, attachmentSrc, filesToAttachments, isPdf, MAX_FILES, MAX_TOTAL_BYTES, totalBytes } from "../lib/attachments";
import { featuredCommands, loadCommands, longRunningCommand, matchCommands } from "../lib/commands";
import type { SlashCommand } from "../lib/commands";
import { fmtDuration } from "../lib/format";
import type { Attachment, ChatMessage } from "../lib/types";
import { IconFile, IconPaperclip, IconStop, IconX } from "../ui/Icons";
import { character, type SendPhase } from "../ui/character";
import { Message } from "./Message";

/** Always visible while a run is live: long LLM calls emit nothing for 30s+,
 * and the elapsed clock shows the console is not dead.
 *
 * `command` is set for a slash command that runs an orchestrator tool. Those
 * runs emit nothing at all, so the generic copy (tool calls as they happen)
 * would be wrong. */
export function TurnRow(props: { command: SlashCommand | null; arrived: boolean; phase: "work" | "done" | "failed" }) {
  const { command, arrived, phase } = props;
  const [since] = useState(() => Date.now());
  return (
    <div className="msg agent activity">
      <div className="meta">
        <span className="live-mk">
          {/* His stage, one mount for the whole turn: the box the send button
              throws lands on its floor (the marker is where it goes), he
              climbs out, works there as the turn goes on, and when it ends
              the SAME stage plays his exit — relief and poof, or despair.
              Two rows (one working, one exiting) unmounted the stage between
              them and it blinked at every turn's end (2026-10-23). */}
          <span className="mk-stage-slot">
            <span className="mk-landing" data-mk-landing />
            {(arrived || phase !== "work") && <character.Turn phase={phase} since={since} />}
          </span>
          {phase === "work" ? <ActivityClock command={command} since={since} />
            : phase === "failed" ? "that one didn't work out" : "done"}
        </span>
      </div>
    </div>
  );
}

/** The row's words and its ticking clock, apart from the stage, so a tick
 *  re-renders a line of text rather than the meeseek. */
function ActivityClock({ command, since }: { command: SlashCommand | null; since: number }) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - since) / 1000)), 1000);
    return () => clearInterval(id);
  }, [since]);
  return (
    <>
      {command ? `/${command.name} running` : "on it"} · {fmtDuration(elapsed)}
      {command
        ? " — a fixed pipeline that reports only when it finishes, usually one to two minutes. Its work runs outside this stream, so the silence until then is expected, not a hang."
        : elapsed >= 45 && " — long runs stream tool calls as they happen; the first run after a server restart is the slowest"}
    </>
  );
}

/** The one hint that is not a command, kept so the empty state still shows that
 *  plain questions work. The command hints come from the server. */
const FREE_TEXT_HINT = "Summarize this sprint";

/** An agent message that only handed off (no text, no question) folds into the
 *  next agent message: one turn reads as one answer, its hand-offs among the
 *  steps, under the agent that answered. */
function turns(messages: ChatMessage[]): ChatMessage[] {
  const out: ChatMessage[] = [];
  for (const m of messages) {
    const prev = out[out.length - 1];
    if (m.role === "agent" && prev?.role === "agent" && !prev.text.trim() && !prev.question && !prev.error) {
      out[out.length - 1] = { ...m, key: prev.key, tools: [...prev.tools, ...m.tools] };
    } else {
      out.push(m);
    }
  }
  return out;
}

/** The conversation, apart from the composer: typing re-renders the composer
 *  only, and a streamed paint re-renders only the message that changed. */
const Thread = memo(function Thread(props: {
  messages: ChatMessage[];
  streaming: boolean;
  readOnly: boolean;
  runningCmd: SlashCommand | null;
  exiting: "done" | "failed" | null;
  arrived: boolean;
  /** Keys the turn's row per send, so a new turn gets a fresh stage. */
  turnKey: number;
  onResume?: (turn: string) => void;
  onAnswer: (answer: string) => void;
}) {
  const { messages, streaming, readOnly } = props;
  const shown = useMemo(() => turns(messages), [messages]);
  return (
    <div className="thread">
      {shown.map((m, i) => (
        <Message
          key={m.key}
          msg={m}
          onResume={i === shown.length - 1 && !streaming ? props.onResume : undefined}
          onAnswer={i === shown.length - 1 && !streaming && !readOnly ? props.onAnswer : undefined}
        />
      ))}
      {(streaming || props.exiting) && (
        <TurnRow key={props.turnKey} command={props.runningCmd} arrived={props.arrived}
                 phase={streaming ? "work" : props.exiting === "failed" ? "failed" : "done"} />
      )}
    </div>
  );
});

export function Chat(props: {
  messages: ChatMessage[];
  streaming: boolean;
  hasSession: boolean;
  /** The open session's id: the Claude session its tasks go to is kept per conversation. */
  sessionId?: string | null;
  onSend: (text: string, attachments: Attachment[]) => void;
  onStop: () => void;
  /** Resume a paused or failed turn; offered on the last message only. */
  onResume?: (turn: string) => void;
  /** Non-empty when the open session belongs to someone else: the composer is
   *  replaced by this note. Empty or absent means the session is writable. */
  readOnlyNote?: string;
}) {
  const { messages, streaming, hasSession } = props;
  const readOnly = !!props.readOnlyNote;
  const [claudeTarget, setClaudeTarget] = useClaudeTarget(props.sessionId ?? null);
  // "A new Claude session" is new once: the session the first task opened is
  // where the follow-ups go (ask_claude's result names it).
  useEffect(() => {
    if (claudeTarget?.session_id !== "new") return;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role !== "agent") continue;
      for (const t of m.tools ?? []) {
        const hit = /^\[Claude session ([0-9a-f-]{36})/.exec(t.result ?? "");
        if (hit) {
          const asked = messages.slice(0, i).reverse().find((x) => x.role === "user")?.text ?? "";
          setClaudeTarget({ session_id: hit[1], title: splitDirective(asked).rest.split("\n")[0].slice(0, 60) || "Claude" });
          return;
        }
      }
    }
  }, [messages, claudeTarget, setClaudeTarget]);
  const [draft, setDraft] = useState("");
  const [atts, setAtts] = useState<Attachment[]>([]);
  const [attError, setAttError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [palIdx, setPalIdx] = useState(0);
  const [palDismissed, setPalDismissed] = useState(false);
  const [featured, setFeatured] = useState<SlashCommand[]>(featuredCommands);
  // The command that started the run in flight, when it streams nothing. Set
  // on every send, so an interrupting message replaces the previous run's note.
  const [runningCmd, setRunningCmd] = useState<SlashCommand | null>(null);
  // The meeseek's exit after a turn: shown for a moment, then gone.
  const [exiting, setExiting] = useState<"done" | "failed" | null>(null);
  // Each send throws the box onto the running turn's stage (`flight` keys
  // one throw); the row shows him climbing out once it has landed there.
  const [flight, setFlight] = useState(0);
  const [arrived, setArrived] = useState(true);
  const [launching, setLaunching] = useState(false);
  const launchTimer = useRef<number | undefined>(undefined);
  const boxRef = useRef<HTMLButtonElement>(null);
  const wasStreaming = useRef(false);
  const exitTimer = useRef<number | undefined>(undefined);
  // Derived during render, not in an effect after it: the commit that ends the
  // stream must already carry the exit, or the turn's row unmounts for a frame
  // between the two states and the stage blinks at every turn's end (2026-10-23).
  const [prevStreaming, setPrevStreaming] = useState(streaming);
  if (streaming !== prevStreaming) {
    setPrevStreaming(streaming);
    setExiting(streaming ? null : messages[messages.length - 1]?.error ? "failed" : "done");
  }
  useEffect(() => {
    if (wasStreaming.current && !streaming) {
      // Kept in a ref, not cleaned up by this effect: messages keep updating
      // after the stream ends, and the exit must still end on time.
      window.clearTimeout(exitTimer.current);
      // long enough for the bridge from where he was and all of joy, poof included
      exitTimer.current = window.setTimeout(() => setExiting(null), messages[messages.length - 1]?.error ? 6000 : 4400);
    }
    wasStreaming.current = streaming;
  }, [streaming, messages]);
  useEffect(() => () => {
    window.clearTimeout(exitTimer.current);
    window.clearTimeout(launchTimer.current);
  }, []);
  const sendPhase: SendPhase = launching ? "launch" : streaming ? "working" : exiting ?? "rest";
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const stickToBottom = useRef(true);

  const palette = palDismissed ? [] : matchCommands(draft);
  // Stable, so the memoized thread is not re-rendered by every parent render.
  const answer = useStableCallback((a: string) => props.onSend(a, []));
  const resume = useStableCallback((id: string) => props.onResume?.(id));

  function pickCommand(name: string) {
    setDraft(`/${name} `);
    setPalDismissed(true);
    inputRef.current?.focus();
  }

  // The empty state can render before the command list has landed, and nothing
  // else re-renders this component when it does. loadCommands shares one
  // request with the app shell's own call, so asking again is free.
  useEffect(() => {
    void loadCommands().then(() => setFeatured(featuredCommands()));
  }, []);

  // The note belongs to the run in flight; the run ending retires it.
  useEffect(() => {
    if (!streaming) setRunningCmd(null);
  }, [streaming]);

  async function attach(files: File[]) {
    if (!files.length) return;
    const { attachments, rejected } = await filesToAttachments(
      files, MAX_FILES - atts.length, totalBytes(atts));
    // Re-checked on merge against BOTH limits: two pastes landing together each
    // saw the same room, so each can pass alone and still breach the total.
    if (attachments.length) {
      setAtts((prev) => {
        const merged = [...prev];
        let spent = totalBytes(prev);
        for (const a of attachments) {
          if (merged.length >= MAX_FILES) break;
          const size = attachmentBytes(a);
          if (spent + size > MAX_TOTAL_BYTES) continue;
          merged.push(a);
          spent += size;
        }
        return merged;
      });
    }
    setAttError(rejected.join(" · "));
  }

  function removeAttachment(i: number) {
    setAtts((prev) => prev.filter((_, j) => j !== i));
    setAttError("");
  }

  // Follow the stream only while the user is already at the bottom.
  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  function send(text?: string) {
    const t = (text ?? draft).trim();
    // An attachment on its own is a valid turn ("what's wrong with this?"), so
    // the guard is text-or-attachment rather than text.
    if ((!t && !atts.length) || !hasSession || readOnly) return; // sending during a run interrupts it (App.send)
    setRunningCmd(longRunningCommand(t));
    setDraft("");
    setAtts([]);
    setAttError("");
    stickToBottom.current = true;
    // A slash command is the command alone; any other message carries the
    // Claude session it goes to, for meeseek to hand over.
    props.onSend(claudeTarget && t && !t.startsWith("/") ? `${directive(claudeTarget)}\n${t}` : t, atts);
    // With a flight (a character's), the turn row waits for it to land.
    setArrived(character.Flight === null);
    setLaunching(true);
    window.clearTimeout(launchTimer.current);
    launchTimer.current = window.setTimeout(() => setLaunching(false), 900);
    setFlight((n) => n + 1);
  }

  return (
    <>
      {messages.length === 0 ? (
        <div className="empty">
          <character.Empty />
          <h2>{character.emptyTitle}</h2>
          <p>{character.emptyLine}</p>
          {/* Served by the backend so the list cannot drift from the
              dispatcher; if the load failed, the free-text hint stands alone. */}
          <div className="hints">
            {featured.map((c) => {
              // A command whose hint reads `<task>` cannot run without one, so
              // clicking it composes the message instead of sending it.
              const needsArgs = c.args.startsWith("<");
              return (
                <button
                  key={c.name}
                  className="hint-cmd"
                  onClick={() => (needsArgs ? pickCommand(c.name) : send(`/${c.name}`))}
                  disabled={!hasSession || streaming || readOnly}
                  title={c.description}
                  aria-label={needsArgs ? `Write a /${c.name} message: ${c.description}` : `Run /${c.name}: ${c.description}`}
                >
                  <span className="cmd">/{c.name}</span>
                  {needsArgs && <span className="args">{c.args}</span>}
                  <span className="desc">{c.description}</span>
                </button>
              );
            })}
            <button
              onClick={() => send(FREE_TEXT_HINT)}
              disabled={!hasSession || streaming || readOnly}
            >
              {FREE_TEXT_HINT}
            </button>
          </div>
        </div>
      ) : (
        <div className="chat-scroll" ref={scrollRef} onScroll={onScroll}>
          <Thread
            messages={messages}
            streaming={streaming}
            readOnly={readOnly}
            runningCmd={runningCmd}
            exiting={exiting}
            arrived={arrived}
            turnKey={flight}
            onResume={props.onResume ? resume : undefined}
            onAnswer={answer}
          />
        </div>
      )}

      {/* A session that belongs to another user is readable but not
          writable: the server would accept a message here, but it would land in their
          history, under their user id. The composer goes rather than being
          disabled — a dead input invites clicking at it. */}
      {readOnly ? (
        <div className="composer-wrap">
          <div className="composer-hint read-only">{props.readOnlyNote}</div>
        </div>
      ) : (
        <div
          className={"composer-wrap" + (dragging ? " dragging" : "")}
          onDragOver={(e) => {
            if (!hasSession || !e.dataTransfer.types.includes("Files")) return;
            e.preventDefault(); // without this the browser navigates to the file
            setDragging(true);
          }}
          onDragLeave={(e) => {
            // Ignore the leave events fired while crossing child elements.
            if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false);
          }}
          onDrop={(e) => {
            if (!hasSession) return;
            e.preventDefault();
            setDragging(false);
            attach([...e.dataTransfer.files]);
          }}
        >
          <div className="composer-anchor">
            {palette.length > 0 && (
              <div className="palette" role="listbox" aria-label="Slash commands">
                {palette.map((c, i) => (
                  <button
                    key={c.name}
                    role="option"
                    aria-selected={i === palIdx}
                    className={"palette-item" + (i === palIdx ? " sel" : "")}
                    onMouseEnter={() => setPalIdx(i)}
                    onMouseDown={(e) => {
                      e.preventDefault(); // keep textarea focus
                      pickCommand(c.name);
                    }}
                  >
                    <span className="cmd">/{c.name}</span>
                    {c.args && <span className="args">{c.args}</span>}
                    <span className="desc">{c.description}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
          {atts.length > 0 && (
            <div className="attachments">
              {atts.map((a, i) => (
                <div className="att" key={`${a.name}-${i}`}>
                  {/* A PDF has no thumbnail to show without rendering it, so it
                      gets a card of the same size instead. */}
                  {isPdf(a) ? (
                    <span className="att-doc" aria-hidden><IconFile />PDF</span>
                  ) : (
                    <img src={attachmentSrc(a)} alt={a.name} />
                  )}
                  <button
                    className="att-remove"
                    onClick={() => removeAttachment(i)}
                    aria-label={`Remove ${a.name}`}
                    title="Remove"
                  >
                    <IconX />
                  </button>
                  <span className="att-name" title={a.name}>{a.name}</span>
                </div>
              ))}
            </div>
          )}
          {attError && <div className="att-error">{attError}</div>}
          {claudeTarget && (
            <div className="claude-target-row">
              <ClaudeTargetChip target={claudeTarget} onClear={() => setClaudeTarget(null)} />
              <span className="claude-target-note">meeseek hands these tasks to it</span>
            </div>
          )}

          <div className="composer">
            <textarea
              ref={inputRef}
              rows={1}
              placeholder={hasSession ? "Message the agent… (/ for commands)" : "Create a session to start"}
              value={draft}
              disabled={!hasSession}
              onChange={(e) => {
                setDraft(e.target.value);
                setPalDismissed(false);
                setPalIdx(0);
                e.target.style.height = "auto";
                e.target.style.height = Math.min(e.target.scrollHeight, 200) + "px";
              }}
              onPaste={(e) => {
                // A screenshot on the clipboard arrives as a file with no text
                // flavour; a copied rich-text region carries both, and there the
                // text is what the user meant, so images only win when alone.
                const files = [...e.clipboardData.files];
                if (!files.length || e.clipboardData.getData("text/plain")) return;
                e.preventDefault();
                attach(files);
              }}
              onKeyDown={(e) => {
                if (palette.length > 0) {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setPalIdx((i) => (i + 1) % palette.length);
                    return;
                  }
                  if (e.key === "ArrowUp") {
                    e.preventDefault();
                    setPalIdx((i) => (i - 1 + palette.length) % palette.length);
                    return;
                  }
                  if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
                    e.preventDefault();
                    pickCommand(palette[Math.min(palIdx, palette.length - 1)].name);
                    return;
                  }
                  if (e.key === "Escape") {
                    e.preventDefault();
                    setPalDismissed(true);
                    return;
                  }
                }
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  send();
                }
              }}
            />
            <input
              ref={fileRef}
              type="file"
              className="att-input"
              accept={ACCEPT_ATTR}
              multiple
              onChange={(e) => {
                attach([...(e.target.files ?? [])]);
                e.target.value = ""; // so picking the same file twice still fires
              }}
            />
            <ClaudeTargetPicker target={claudeTarget} onPick={setClaudeTarget} disabled={!hasSession} />
            <button
              className="attach"
              onClick={() => fileRef.current?.click()}
              disabled={!hasSession || atts.length >= MAX_FILES}
              aria-label="Attach an image or a PDF"
              title={
                atts.length >= MAX_FILES
                  ? `${MAX_FILES} files per message is the limit`
                  : "Attach an image or a PDF — you can also paste or drop one"
              }
            >
              <IconPaperclip />
            </button>
            {streaming && (
              <button className="stop" onClick={props.onStop} aria-label="Pause the run" title="Pause (resume it later)">
                <IconStop />
              </button>
            )}
            <button
              ref={boxRef}
              className={`send ${character.sendClass}`.trim()}
              onClick={() => send()}
              disabled={(!draft.trim() && !atts.length) || !hasSession}
              aria-label={streaming ? "Send — stops this turn and starts a new one" : "Send message"}
              title={character.sendTitle(streaming)}
            >
              <character.Send phase={sendPhase} />
            </button>
          </div>
          {/* Always there, so a run starting or ending changes its words, not
              the height of everything above it. */}
          <div className="composer-hint">
            {dragging
              ? "Drop the file to attach it"
              : streaming
                ? "Enter sends and stops this turn · a paused turn can be resumed"
                : "Enter sends · Shift+Enter for a new line · / for commands"}
          </div>
          {character.Flight && flight > 0 && !arrived && (
            <character.Flight key={flight} from={boxRef} onLand={() => setArrived(true)} />
          )}
        </div>
      )}
    </>
  );
}
