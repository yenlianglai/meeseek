// node --test frontend/tests
import assert from "node:assert/strict";
import { test } from "node:test";
import { normalizeMath } from "../src/markdown/math.ts";

test("a $$ line of its own becomes display math", () => {
  assert.equal(normalizeMath("前文\n$$ f(x) = x^2 $$\n後文"), "前文\n$$\nf(x) = x^2\n$$\n後文");
});

test("\\[ \\] and \\( \\) become dollar math", () => {
  assert.equal(normalizeMath("見 \\[a+b\\] 與 \\( c \\)"), "見 \n$$\na+b\n$$\n 與 $c$");
});

test("inline $…$ math is kept", () => {
  assert.equal(normalizeMath("複雜度 $O(n^2)$ 而已"), "複雜度 $O(n^2)$ 而已");
});

test("money is not math", () => {
  assert.equal(normalizeMath("US$40.41，約 NT$1,293"), "US\\$40.41，約 NT\\$1,293");
  assert.equal(normalizeMath("costs $5 and $10"), "costs \\$5 and \\$10");
});

test("code is left alone", () => {
  const fenced = "```bash\necho $HOME $PATH\n```";
  assert.equal(normalizeMath(fenced), fenced);
  assert.equal(normalizeMath("run `echo $HOME` now"), "run `echo $HOME` now");
});

test("text without math is returned as is", () => {
  assert.equal(normalizeMath("沒有公式"), "沒有公式");
});
