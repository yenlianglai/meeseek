// node --test frontend/tests  (Node 22.6+ runs TypeScript directly)
import assert from "node:assert/strict";
import { test } from "node:test";

const store: Record<string, string> = {};
(globalThis as unknown as { localStorage: unknown }).localStorage = {
  getItem: (k: string) => store[k] ?? null,
  setItem: (k: string, v: string) => { store[k] = v; },
};
const { sessionTitle, setTitle } = await import("../src/lib/titles.ts");

const row = (id: string, title: string) => ({ id, user: "webui", title, updated: 0 });

test("the Claude-session line a message leads with is never its title", () => {
  assert.equal(sessionTitle(row("a", "[Claude session: 081c3426-edda · calc.py 的 add 函式]\n再加一個測試")), "再加一個測試");
  assert.equal(sessionTitle(row("b", "")), "New session");
});

test("a rename wins over the first message", () => {
  setTitle("c", "週報");
  assert.equal(sessionTitle(row("c", "寫週報")), "週報");
});
