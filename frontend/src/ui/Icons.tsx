/** Drawn icon set — one 1.5px stroke weight throughout. */

const base = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.5,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

export function IconPlus() {
  return (
    <svg {...base} aria-hidden>
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

export function IconStop() {
  return (
    <svg {...base} aria-hidden>
      <rect x="7" y="7" width="10" height="10" rx="1.5" />
    </svg>
  );
}

export function IconSend() {
  return (
    <svg {...base} aria-hidden>
      <path d="M5 12h13M13 6l6 6-6 6" />
    </svg>
  );
}

export function IconChat() {
  return (
    <svg {...base} aria-hidden>
      <path d="M21 12a8 8 0 0 1-8 8H5.5L3 22.5V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z" />
    </svg>
  );
}

export function IconTrace() {
  return (
    <svg {...base} aria-hidden>
      <path d="M4 6h10M4 12h16M4 18h7" />
      <circle cx="18" cy="6" r="1.6" />
      <circle cx="14.5" cy="18" r="1.6" />
    </svg>
  );
}

export function IconSearch() {
  return (
    <svg {...base} width={14} height={14} aria-hidden>
      <circle cx="11" cy="11" r="6" />
      <path d="M20 20l-4.5-4.5" />
    </svg>
  );
}

export function IconTrash() {
  return (
    <svg {...base} width={14} height={14} aria-hidden>
      <path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12" />
    </svg>
  );
}

export function IconChart() {
  return (
    <svg {...base} aria-hidden>
      <path d="M4 20h16" />
      <path d="M7 20v-6M12 20V6M17 20v-9" />
    </svg>
  );
}

export function IconChevron() {
  return (
    <svg {...base} width={14} height={14} aria-hidden>
      <path d="M9 6l6 6-6 6" />
    </svg>
  );
}

export function IconMenu() {
  return (
    <svg {...base} aria-hidden>
      <path d="M4 7h16M4 12h16M4 17h16" />
    </svg>
  );
}

export function IconAlert() {
  return (
    <svg {...base} width={15} height={15} aria-hidden style={{ flex: "none", marginTop: 2 }}>
      <path d="M12 9v4M12 16.5v.5" />
      <path d="M10.3 4.1 2.9 17a2 2 0 0 0 1.7 3h14.8a2 2 0 0 0 1.7-3L13.7 4.1a2 2 0 0 0-3.4 0Z" />
    </svg>
  );
}

export function IconCopy() {
  return (
    <svg {...base} width={13} height={13} aria-hidden>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M15 5.5A1.5 1.5 0 0 0 13.5 4h-8A1.5 1.5 0 0 0 4 5.5v8A1.5 1.5 0 0 0 5.5 15" />
    </svg>
  );
}

export function IconCheck() {
  return (
    <svg {...base} width={13} height={13} aria-hidden>
      <path d="M5 12.5 10 17.5 19 7" />
    </svg>
  );
}

export function IconImage() {
  return (
    <svg {...base} width={13} height={13} aria-hidden>
      <rect x="3.5" y="5" width="17" height="14" rx="2" />
      <circle cx="9" cy="10" r="1.4" />
      <path d="m4.5 17 4.7-4.3a1.6 1.6 0 0 1 2.2 0L19.5 19" />
    </svg>
  );
}

/** The attach button: a clip reads as "any file" — the composer takes images and PDFs. */
export function IconPaperclip() {
  return (
    <svg {...base} width={13} height={13} aria-hidden>
      <path d="M18.5 11.5 12 18a4.2 4.2 0 0 1-6-6l6.6-6.6a2.9 2.9 0 0 1 4.1 4.1l-6.6 6.6a1.6 1.6 0 0 1-2.2-2.2l6-6" />
    </svg>
  );
}

/** Stands in for a document that has no thumbnail — a PDF, in practice. */
export function IconFile() {
  return (
    <svg {...base} width={13} height={13} aria-hidden>
      <path d="M14 3.5H7a1.5 1.5 0 0 0-1.5 1.5v14A1.5 1.5 0 0 0 7 20.5h10a1.5 1.5 0 0 0 1.5-1.5V8z" />
      <path d="M14 3.5V8h4.5" />
    </svg>
  );
}

export function IconDownload() {
  return (
    <svg {...base} width={13} height={13} aria-hidden>
      <path d="M12 4v10" />
      <path d="m7.5 10 4.5 4.5 4.5-4.5" />
      <path d="M4.5 18.5h15" />
    </svg>
  );
}

export function IconPencil() {
  return (
    <svg {...base} width={14} height={14} aria-hidden>
      <path d="M4 20h4L19.2 8.8a2 2 0 0 0 0-2.8l-1.2-1.2a2 2 0 0 0-2.8 0L4 16v4Z" />
      <path d="M14.5 6.5 17.5 9.5" />
    </svg>
  );
}

export function IconX() {
  return (
    <svg {...base} width={13} height={13} aria-hidden>
      <path d="M6 6l12 12M18 6 6 18" />
    </svg>
  );
}

/** The meeseek mark: one task in brackets, done. The brackets take the ink,
 *  the check the signal colour. */
export function Logo(props: { className?: string }) {
  return (
    <svg
      className={props.className}
      viewBox="0 0 24 24"
      fill="none"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M7.5 4.5H4.5v15h3M16.5 4.5h3v15h-3" stroke="var(--ink)" strokeWidth={1.8} />
      <path d="M8.6 12.4 11 14.8l4.6-5.2" stroke="var(--accent)" strokeWidth={2.2} />
    </svg>
  );
}

export function IconLearning() {
  // A book: what the team has written down for next time.
  return (
    <svg {...base} aria-hidden>
      <path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5z" />
      <path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H13v16h5.5a1.5 1.5 0 0 0 1.5-1.5z" />
    </svg>
  );
}

export function IconClaude() {
  // A burst: the stronger hand meeseek passes work to.
  return (
    <svg {...base} aria-hidden>
      <path d="M12 3v18M3 12h18M5.6 5.6l12.8 12.8M18.4 5.6 5.6 18.4" />
    </svg>
  );
}

export function IconInbox() {
  return (
    <svg {...base} aria-hidden>
      <path d="M4 13l2.5-7h11L20 13" />
      <path d="M4 13v5h16v-5h-4.5l-1.5 2.5h-4L8.5 13z" />
    </svg>
  );
}

export function IconSun() {
  return (
    <svg {...base} aria-hidden>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4" />
    </svg>
  );
}

export function IconMoon() {
  return (
    <svg {...base} aria-hidden>
      <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z" />
    </svg>
  );
}
