import { ClaudeTargetChip, splitDirective } from "./ClaudeTarget";
import { type ComponentPropsWithoutRef, memo, useMemo, useRef, useState } from "react";
import { attachmentSrc, isPdf } from "../lib/attachments";
import { copyRich, copyText, tableToClipboard } from "../lib/clipboard";
import { noteCopy, rateTurn } from "../lib/api";
import { agentLabel } from "../lib/format";
import type { ChatMessage, ToolCall } from "../lib/types";
import { CodeBlock, CopyableImage, extractText, HtmlBlock, MermaidBlock } from "./Artifacts";
import { CopyButton } from "../ui/Copy";
import { Md } from "../markdown/Md";
import { OutputLinks, useOpenOutput } from "./OutputViewer";
import { isPage, PageFrame } from "./PageFrame";
import { outputPaths } from "../lib/outputs";
import { IconAlert, IconChevron, IconFile } from "../ui/Icons";
import { character } from "../ui/character";

/** Fenced blocks route by language: mermaid → live diagram, html → sandboxed
 * preview toggle, everything else → a highlighted block with its own copy. */
function Pre(props: ComponentPropsWithoutRef<"pre">) {
  const child = Array.isArray(props.children) ? props.children[0] : props.children;
  const cp = (child && typeof child === "object" && "props" in child
    ? (child as { props: { className?: string; children?: React.ReactNode } }).props
    : undefined);
  const lang = /language-(\w+)/.exec(cp?.className ?? "")?.[1];
  const code = extractText(cp?.children);
  if (lang === "mermaid") return <MermaidBlock code={code} />;
  if (lang === "html") return <HtmlBlock code={code} />;
  return (
    <CodeBlock code={code} lang={lang}>
      <pre {...props} />
    </CodeBlock>
  );
}

/**
 * Tables are the artifact people most often want *out* of the console — a
 * sprint breakdown belongs in a spreadsheet. Copying reads the rendered table
 * and emits TSV plus HTML, so one paste lands as real cells in Sheets and as a
 * real table in a doc.
 */
function Table(props: ComponentPropsWithoutRef<"table">) {
  const ref = useRef<HTMLTableElement>(null);
  return (
    <div className="table-block">
      <div className="table-actions">
        <CopyButton
          label="Copy table"
          title="Copy the table, ready to paste into a spreadsheet or a doc"
          onCopy={() => {
            const el = ref.current;
            if (!el) return false;
            const { html, plain } = tableToClipboard(el);
            return copyRich(html, plain);
          }}
        />
      </div>
      <div className="table-scroll">
        <table ref={ref} {...props} />
      </div>
    </div>
  );
}

function argsPreview(args?: Record<string, unknown>): string {
  if (!args || Object.keys(args).length === 0) return "";
  const s = Object.entries(args)
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");
  return s.length > 80 ? s.slice(0, 80) + "…" : s;
}

/**
 * One line of the payload, so a collapsed chip still says what came back.
 * Indented JSON would preview as a lone "{", so collapse it back to one line
 * first — the expanded body keeps the readable indented form.
 */
function resultPreview(tool: ToolCall): string {
  if (!tool.done) return "";
  // Only the head can show, so only the head is scanned: a payload can be
  // megabytes, and this runs on every render of the chip.
  const raw = (tool.result ?? "").slice(0, 400).trim();
  if (!raw) return "no output";
  const text = /^[{[]/.test(raw) ? raw.replace(/\s+/g, " ") : raw.split("\n")[0];
  return text.length > 70 ? text.slice(0, 70) + "…" : text;
}

/** What an agent did on the way to its answer, folded to one line: the step
 *  running now, or how many there were and which tools. The answer is what the
 *  reader came for; the steps open on demand. */
function Steps({ tools }: { tools: ToolCall[] }) {
  const running = tools.find((t) => !t.done);
  const failed = tools.filter((t) => t.error).length;
  const names = [...new Set(tools.map((t) =>
    t.name === "transfer_to_agent" ? `→ ${agentLabel(String(t.args?.agent_name ?? ""))}` : t.name))];
  // What the steps saved, openable without unfolding them.
  const outputs = useMemo(() => [...new Set(tools.flatMap((t) => outputPaths(t.result)))].slice(0, 3), [tools]);
  const opened = useOpened();
  return (
    <details className="steps" onToggle={opened.onToggle}>
      <summary>
        {running ? <span className="spin" aria-label="running" /> : <span className="chev"><IconChevron /></span>}
        <span className="steps-count">
          {running ? `${running.name}…` : `${tools.length} ${tools.length === 1 ? "step" : "steps"}`}
        </span>
        <span className="steps-names">{names.join(" · ")}</span>
        {failed > 0 && <span className="steps-err">{failed} failed</span>}
        <OutputLinks paths={outputs} />
      </summary>
      {opened.ever && (
        <div className="tools">
          {tools.map((t, i) => <Tool key={i} tool={t} />)}
        </div>
      )}
    </details>
  );
}

/** A folded section's body is mounted the first time it is opened, not before:
 *  a closed one costs nothing to keep, however much it holds. */
function useOpened() {
  const [ever, setEver] = useState(false);
  return { ever, onToggle: (e: React.SyntheticEvent<HTMLDetailsElement>) => {
    if (e.currentTarget.open) setEver(true);
  } };
}

function Tool({ tool }: { tool: ToolCall }) {
  const preview = resultPreview(tool);
  const paths = useMemo(() => outputPaths(tool.result).slice(0, 3), [tool.result]);
  const opened = useOpened();
  return (
    <details className={tool.error ? "tool err" : "tool"} onToggle={opened.onToggle}>
      <summary>
        {!tool.done && <span className="spin" aria-label="running" />}
        <span className="name">{tool.name}</span>
        <span className="args">{argsPreview(tool.args)}</span>
        {preview && <span className="preview">→ {preview}</span>}
        <OutputLinks paths={paths} />
        <span className="chev">
          <IconChevron />
        </span>
      </summary>
      {opened.ever && <div className="tool-body">
        {tool.result && (
          <div className="tool-actions">
            <CopyButton
              title={`Copy the ${tool.name} payload`}
              onCopy={() => copyText(tool.result ?? "")}
            />
          </div>
        )}
        <pre className="result">{tool.result || (tool.done ? "(no output)" : "Running…")}</pre>
      </div>}
    </details>
  );
}

/** The web pages a reply made or names, live below it (the first two). */
function Pages({ msg }: { msg: ChatMessage }) {
  const paths = [...new Set([msg.text, ...msg.tools.map((t) => t.result)].flatMap((t) => outputPaths(t)))]
    .filter(isPage).slice(0, 2);
  return <>{paths.map((p) => <PageFrame key={p} path={p} />)}</>;
}

/** Inline code naming an agent output opens it; any other code is code. */
function Code(props: ComponentPropsWithoutRef<"code">) {
  const open = useOpenOutput();
  const [path] = props.className ? [] : outputPaths(extractText(props.children));
  if (!path) return <code {...props} />;
  return (
    <button type="button" className="code-link" title="Open this output" onClick={() => open(path)}>
      <code {...props} />
    </button>
  );
}

const MD_COMPONENTS = {
  pre: Pre,
  code: Code,
  table: Table,
  img: (p: ComponentPropsWithoutRef<"img">) => (
    <CopyableImage src={p.src} alt={p.alt} title={p.title} />
  ),
} satisfies Record<string, unknown>;

/** What you wrote, with the Claude session it was handed to shown as a chip. */
function UserText({ text }: { text: string }) {
  const { target, rest } = splitDirective(text);
  return (
    <>
      {target && <div className="sent-target"><ClaudeTargetChip target={target} /></div>}
      {rest && <div className="body">{rest}</div>}
    </>
  );
}

/** One message of the thread. Memoized on `msg`'s identity: the live turn is
 *  copied on every paint (App's runTurn), so only the message that changed
 *  re-renders while a reply streams. */
export const Message = memo(function Message({ msg, onResume, onAnswer }: {
  msg: ChatMessage;
  onResume?: (turn: string) => void;
  onAnswer?: (answer: string) => void;
}) {
  // Your 👍/👎 (runtime/feedback.py). 👎 asks what was wrong first; Send or Skip records it once.
  const [rated, setRated] = useState<0 | 1 | -1>(0);
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");
  const rate = (r: 1 | -1, why = "") => {
    if (!msg.turn) return;
    setRated(r);
    setAsking(false);
    rateTurn(msg.turn, r, why).catch(() => setRated(0));
  };
  if (msg.role === "user") {
    return (
      <div className="msg user">
        <div className="meta">
          <span className="author">You</span>
          {msg.text && (
            <span className="msg-actions">
              <CopyButton title="Copy this message" onCopy={() => copyText(msg.text)} />
            </span>
          )}
        </div>
        {msg.attachments && msg.attachments.length > 0 && (
          <div className="sent-images">
            {msg.attachments.map((a, i) =>
              isPdf(a) ? (
                // A data: URL cannot be opened as a top-level page in Chrome, so
                // the chip downloads the PDF rather than pretending to open it.
                <a
                  key={i}
                  className="sent-doc"
                  href={attachmentSrc(a)}
                  download={a.name.endsWith(".pdf") ? a.name : `${a.name}.pdf`}
                  title={`${a.name} — download the PDF you sent`}
                >
                  <IconFile />
                  {a.name}
                </a>
              ) : (
                <CopyableImage key={i} src={attachmentSrc(a)} alt={a.name} title={a.name} />
              ),
            )}
          </div>
        )}
        {msg.text && <UserText text={msg.text} />}
      </div>
    );
  }
  return (
    <div className="msg agent">
      <div className="meta">
        <span className="author"><character.AuthorMark error={!!msg.error} />{agentLabel(msg.author)}</span>
        {msg.streaming && msg.tools.some((t) => !t.done) && (
          <span className="working">{msg.tools.find((t) => !t.done)!.name}…</span>
        )}
        {/* Held back until the run finishes: copying a half-streamed answer
            produces a truncated paste that looks complete. */}
        {msg.text && !msg.streaming && (
          <span className="msg-actions">
            {msg.turn && (
              <>
                <button type="button" className={`copy-btn icon-only rate${rated === 1 ? " on" : ""}`}
                        title="Good answer" aria-label="Good answer" aria-pressed={rated === 1}
                        onClick={() => rate(1)}>👍</button>
                <button type="button" className={`copy-btn icon-only rate${rated === -1 || asking ? " on" : ""}`}
                        title="Not good — say what was wrong" aria-label="Not a good answer"
                        aria-pressed={rated === -1} onClick={() => setAsking(true)}>👎</button>
              </>
            )}
            <CopyButton
              title="Copy this answer as markdown"
              onCopy={() => {
                if (msg.turn) noteCopy(msg.turn);
                return copyText(msg.text);
              }}
            />
          </span>
        )}
      </div>
      {msg.tools.length > 0 && <Steps tools={msg.tools} />}
      {msg.text && (
        <div className="md">
          <Md components={MD_COMPONENTS as never}>{msg.text}</Md>
        </div>
      )}
      {asking && (
        <form className="rate-note" onSubmit={(e) => { e.preventDefault(); rate(-1, note.trim()); }}>
          <input autoFocus value={note} onChange={(e) => setNote(e.target.value)}
                 placeholder="What was wrong? (optional — meeseek learns from it)" aria-label="What was wrong" />
          <button type="submit" className="btn-ghost small">Send</button>
          <button type="button" className="btn-ghost small" onClick={() => rate(-1)}>Skip</button>
        </form>
      )}
      {!msg.streaming && <Pages msg={msg} />}
      {msg.question && (
        <div className="question">
          <div className="q">{msg.question.question}</div>
          {msg.question.recommended && (
            <div className="rec">Suggested: {msg.question.recommended}</div>
          )}
          {onAnswer && msg.question.options.length > 0 && (
            <div className="options">
              {msg.question.options.map((o) => (
                <button type="button" key={o} onClick={() => onAnswer(o)}>
                  {o}
                </button>
              ))}
            </div>
          )}
          {onAnswer && <div className="hint">Or type your own answer below</div>}
        </div>
      )}
      {msg.notice && <div className="notice-inline">{msg.notice}</div>}
      {msg.error && (
        <div className="error">
          <IconAlert />
          <span>
            The agent run failed: {msg.error}.
            {msg.interrupted ? " Resume to carry on from where it stopped." : " Try again, or start a fresh session if it keeps failing."}
          </span>
        </div>
      )}
      {msg.interrupted && onResume && msg.turn && (
        <div className="resume-row">
          <span>This turn stopped before it finished.</span>
          <button type="button" className="resume" onClick={() => onResume(msg.turn!)}>
            Resume
          </button>
        </div>
      )}
    </div>
  );
});
