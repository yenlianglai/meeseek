/** Inbox sessions you have opened, which the rail lists beside your own.
 *
 * Every Inbox trigger starts a session, so the rail does not list them all —
 * only the ones you have opened and so may carry on. Per-browser, like titles. */

const KEY = "meeseek-inbox-opened";

export function openedInbox(): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(KEY) || "[]"));
  } catch {
    return new Set();
  }
}

/** Remember an opened Inbox session; true when it was not on the rail yet. */
export function markInboxOpened(id: string): boolean {
  const ids = openedInbox();
  if (ids.has(id)) return false;
  ids.add(id);
  try { localStorage.setItem(KEY, JSON.stringify([...ids])); } catch { /* storage off */ }
  return true;
}
