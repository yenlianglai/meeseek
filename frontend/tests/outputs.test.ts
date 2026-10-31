// node --test frontend/tests
import assert from "node:assert/strict";
import { test } from "node:test";
import { outputPaths } from "../src/lib/outputs.ts";

test("a shipped report is found", () => {
  assert.deepEqual(outputPaths("Shipped -> knowledge_base/outputs/reports/20261010_login_timeout.md"),
    ["reports/20261010_login_timeout.md"]);
});

test("delegation results and Chinese punctuation", () => {
  assert.deepEqual(outputPaths("完整產出：outputs/delegations/20261010-3bcf4efb-spec.md。另見 `outputs/images/a.png`"),
    ["delegations/20261010-3bcf4efb-spec.md", "images/a.png"]);
});

test("each path once, and nothing that is not a file", () => {
  assert.deepEqual(outputPaths("outputs/reviews/x.md outputs/reviews/x.md outputs/reviews/"), ["reviews/x.md"]);
  assert.deepEqual(outputPaths(undefined), []);
});
