/** Session titles: the server's (the first message), or the user's rename.
 *
 * Renames live in localStorage — per-browser is enough for a personal tool. */

import type { SessionRow } from "./types";

const KEY = "meeseek-session-titles"; // user renames (authoritative)

// The rail asks for a title per row on every render, so a map is parsed once
// per change of its stored string, not per call. Comparing the string (rather
// than trusting the cache) keeps another tab's writes visible.
const parsed = new Map<string, { raw: string; map: Record<string, string> }>();

/** The stored map — shared, so read it, never write into it. */
function read(key: string): Readonly<Record<string, string>> {
  let raw = "{}";
  try { raw = localStorage.getItem(key) || "{}"; } catch { /* storage off */ }
  const hit = parsed.get(key);
  if (hit && hit.raw === raw) return hit.map;
  let map: Record<string, string> = {};
  try { map = JSON.parse(raw); } catch { /* corrupt: start over */ }
  parsed.set(key, { raw, map });
  return map;
}

/** A copy of the renames, to change and save. */
function load(): Record<string, string> {
  return { ...read(KEY) };
}

function save(map: Record<string, string>): void {
  localStorage.setItem(KEY, JSON.stringify(map));
}

// The Claude-session line a message may lead with is routing, not the topic.
const ROUTING = /^\[Claude session: [^\]\n]*\]\s*/;
const topic = (text: string) => text.replace(ROUTING, "").trim();

/** A rename, if the user gave one. */
export function getTitle(id: string): string | undefined {
  return read(KEY)[id];
}

export function setTitle(id: string, title: string): void {
  const map = load();
  const trimmed = title.trim();
  if (trimmed) map[id] = trimmed;
  else delete map[id];
  save(map);
}

export function clearTitle(id: string): void {
  const map = load();
  delete map[id];
  save(map);
}

/** The user's rename wins; else the server's title — the first message — less
 * a routing line it may lead with. Shared by the rail and the top bar so one
 * session never carries two names. */
export function sessionTitle(s: SessionRow): string {
  return getTitle(s.id) ?? (topic(s.title ?? "") || "New session");
}
