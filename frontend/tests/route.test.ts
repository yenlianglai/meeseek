// node --test frontend/tests  (Node 22.6+ runs TypeScript directly)
import assert from "node:assert/strict";
import { test } from "node:test";
import { formatRoute, parseRoute, type Route } from "../src/lib/route.ts";

const cases: [string, Route][] = [
  ["#/chat", { view: "chat", session: undefined, owner: undefined }],
  ["#/chat/abc", { view: "chat", session: "abc", owner: undefined }],
  ["#/trace/abc?as=mcp%3Aclaude-code", { view: "trace", session: "abc", owner: "mcp:claude-code" }],
  ["#/usage?days=7", { view: "usage", days: 7 }],
  ["#/context/rules", { view: "context", kind: "rules", path: undefined }],
  ["#/context/notes/sync%2F2026-10-10.md", { view: "context", kind: "notes", path: "sync/2026-10-10.md" }],
  ["#/inbox", { view: "inbox", item: undefined }],
  ["#/inbox/8fe38eb5a1", { view: "inbox", item: "8fe38eb5a1" }],
  ["#/work", { view: "work", id: undefined }],
  ["#/work/PROJ-12", { view: "work", id: "PROJ-12" }],
];

test("every route survives a round trip through the URL", () => {
  for (const [hash, route] of cases) {
    assert.deepEqual(parseRoute(hash), route, hash);
    assert.deepEqual(parseRoute(formatRoute(route)), route, hash);
  }
});

test("the console's own owner is the default and never written", () => {
  assert.equal(formatRoute({ view: "chat", session: "s", owner: "webui" }), "#/chat/s");
  assert.equal(parseRoute("#/chat/s?as=webui").owner, undefined);
});

test("the old Learning link opens the rules", () => {
  assert.deepEqual(parseRoute("#/learning"), { view: "context", kind: "rules" });
});

test("anything unknown lands in Chat", () => {
  for (const h of ["", "#", "#/", "#/nope/1", "#/usage?days=-1"]) {
    const r = parseRoute(h);
    assert.ok(r.view === "chat" || (r.view === "usage" && r.days === undefined), h);
  }
});

