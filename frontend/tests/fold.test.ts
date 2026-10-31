import assert from "node:assert/strict";
import { test } from "node:test";
import { foldEvents } from "../src/lib/fold.ts";
import type { ConsoleEvent } from "../src/lib/types.ts";

const at = 1;
const said = (text: string, answers = ""): ConsoleEvent =>
  ({ kind: "said", turn: "t1", at, by: "webui", text, files: [], answers });

test("a step's calls take their results by call id, and the reply ends the turn", () => {
  const msgs = foldEvents([
    said("issues?"),
    { kind: "step", turn: "t1", at, by: "sprint", text: "",
      calls: [{ id: "c1", name: "jira_search", args: { jql: "x" } }] },
    { kind: "result", turn: "t1", at, by: "sprint", call: { id: "c1", name: "jira_search" },
      text: "3 issues", error: false, held: false },
    { kind: "reply", turn: "t1", at, by: "sprint", text: "There are 3." },
  ]);
  assert.equal(msgs.length, 2);
  const [, agent] = msgs;
  assert.equal(agent.text, "There are 3.");
  assert.equal(agent.turn, "t1");
  assert.deepEqual(agent.tools.map((t) => [t.name, t.result, t.done]), [["jira_search", "3 issues", true]]);
  assert.ok(!agent.interrupted);
});

test("a turn whose last word is a call stopped: it is offered to resume", () => {
  const msgs = foldEvents([
    said("deploy"),
    { kind: "step", turn: "t1", at, by: "ops", text: "", calls: [{ id: "c1", name: "k8s_status", args: {} }] },
  ]);
  assert.equal(msgs[1].interrupted, true);
});

test("an ask_user call is a question, settled by the answer", () => {
  const ask: ConsoleEvent = { kind: "step", turn: "t1", at, by: "ops", text: "",
    calls: [{ id: "c1", name: "ask_user", args: { question: "Which env?", options: ["dev", "prod"] } }] };
  const waiting = foldEvents([said("deploy"), ask]);
  assert.equal(waiting[1].question?.question, "Which env?");
  assert.ok(!waiting[1].interrupted); // waiting on the person, not stopped
  const answered = foldEvents([said("deploy"), ask, said("dev", "c1")]);
  assert.equal(answered[1].question, undefined);
  assert.equal(answered[2].text, "dev");
});

test("a message's files come back as links", () => {
  const [m] = foldEvents([{ ...said("look"), files: [{ mime: "image/png", url: "/api/files/abc" }] } as ConsoleEvent]);
  assert.equal(m.attachments?.[0].url, "/api/files/abc");
});
