import { type ReactNode, useEffect, useRef, useState } from "react";
import { IconCheck, IconCopy } from "./Icons";

type Status = "idle" | "ok" | "fail";

/**
 * One copy control, used for every artifact in the thread.
 *
 * A failed copy and a working one look identical until the paste, so the
 * button reports back — a check on success, a named reason on failure — and
 * settles after two seconds.
 */
export function CopyButton(props: {
  /** Performs the copy; resolve false (or throw) to show the failure state. */
  onCopy: () => boolean | Promise<boolean>;
  /** Resting label. Omit for an icon-only button. */
  label?: string;
  /** Accessible name and tooltip, e.g. "Copy the diagram as a PNG image". */
  title: string;
  /** Verb shown on success. Defaults to "Copied". */
  done?: string;
  icon?: ReactNode;
  className?: string;
}) {
  const { label, title, done = "Copied", icon, className = "" } = props;
  const [status, setStatus] = useState<Status>("idle");
  const [reason, setReason] = useState("");
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  async function run() {
    window.clearTimeout(timer.current);
    try {
      const ok = await props.onCopy();
      setStatus(ok ? "ok" : "fail");
      if (!ok) setReason("This browser blocked the copy — select the text and use ⌘C.");
    } catch (e) {
      setStatus("fail");
      setReason(e instanceof Error ? e.message : String(e));
    }
    timer.current = window.setTimeout(() => setStatus("idle"), 2000);
  }

  const text = status === "ok" ? done : status === "fail" ? "Failed" : label;
  return (
    <button
      type="button"
      className={`copy-btn ${status} ${label ? "" : "icon-only"} ${className}`.trim()}
      onClick={run}
      title={status === "fail" ? reason : title}
      // A visible label is the name (what voice control users say); the
      // title stays the longer description. Icon-only takes the title.
      aria-label={label ? undefined : title}
    >
      <span className="copy-icon" aria-hidden>
        {status === "ok" ? <IconCheck /> : (icon ?? <IconCopy />)}
      </span>
      {text && <span className="copy-label">{text}</span>}
      {/* Announced only on a state change, so the confirmation reaches a
          screen reader without the button re-announcing itself at rest. */}
      <span className="sr-only" role="status">
        {status === "ok" ? `${done}.` : status === "fail" ? reason : ""}
      </span>
    </button>
  );
}

/** The bar across the top of an artifact: what it is on the left, what you can
 * take away from it on the right. */
export function ArtifactBar(props: { label: string; children?: ReactNode }) {
  return (
    <div className="artifact-bar">
      <span className="artifact-kind">{props.label}</span>
      <span className="artifact-actions">{props.children}</span>
    </div>
  );
}
