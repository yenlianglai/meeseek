/**
 * Math in model answers, made parseable by remark-math.
 *
 * Models write LaTeX four ways: `$$…$$` on its own line, `\[…\]`, `\(…\)` and
 * `$…$`. remark-math reads only the dollar forms, and treats a `$$…$$` that
 * shares a line with nothing else as inline math. A bare `$` is also money
 * ("US$40", "$5 and $10"), so a single `$` counts as math only by pandoc's
 * rule: the opener is followed by a non-space, the closer is preceded by a
 * non-space and not followed by a digit. Every other `$` is escaped. Code
 * (fenced blocks and inline spans) is left untouched.
 */

const FENCE = /^ {0,3}(`{3,}|~{3,})/;

export function normalizeMath(text: string): string {
  if (!text.includes("$") && !text.includes("\\[") && !text.includes("\\(")) return text;
  const out: string[] = [];
  let prose: string[] = [];
  let fence = "";
  const flush = () => {
    if (prose.length) out.push(normalizeProse(prose.join("\n")));
    prose = [];
  };
  for (const line of text.split("\n")) {
    const m = FENCE.exec(line);
    if (fence) {
      out.push(line);
      if (m && m[1][0] === fence[0] && m[1].length >= fence.length) fence = "";
    } else if (m) {
      flush();
      fence = m[1];
      out.push(line);
    } else {
      prose.push(line);
    }
  }
  flush();
  return out.join("\n");
}

/** Prose with no fenced code: inline code spans are kept as they are. */
function normalizeProse(text: string): string {
  return text
    .split(/(`+[^`\n]*`+)/)
    .map((piece, i) => (i % 2 ? piece : normalizeText(piece)))
    .join("");
}

function normalizeText(text: string): string {
  const converted = text
    .replace(/\\\[([\s\S]+?)\\\]/g, (_, body: string) => `\n$$\n${body.trim()}\n$$\n`)
    .replace(/\\\((.+?)\\\)/g, (_, body: string) => `$${body.trim()}$`)
    .replace(/^[ \t]*\$\$(?!\s*$)(.+?)\$\$[ \t]*$/gm, (_, body: string) => `$$\n${body.trim()}\n$$`);
  return escapeLoneDollars(converted);
}

function escapeLoneDollars(text: string): string {
  let out = "";
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (c === "\\") {
      out += text.slice(i, i + 2);
      i += 2;
      continue;
    }
    if (c !== "$") {
      out += c;
      i += 1;
      continue;
    }
    if (text[i + 1] === "$") {
      const end = text.indexOf("$$", i + 2);
      const stop = end < 0 ? text.length : end + 2;
      out += text.slice(i, stop);
      i = stop;
      continue;
    }
    const close = closingDollar(text, i);
    if (close < 0) {
      out += "\\$";
      i += 1;
    } else {
      out += text.slice(i, close + 1);
      i = close + 1;
    }
  }
  return out;
}

/** Index of the `$` closing inline math opened at `open`, or -1. */
function closingDollar(text: string, open: number): number {
  const first = text[open + 1];
  if (first === undefined || /\s/.test(first)) return -1;
  for (let j = open + 1; j < text.length; j++) {
    const c = text[j];
    if (c === "\\") {
      j += 1;
      continue;
    }
    if (c === "\n" && text[j + 1] === "\n") return -1; // math never spans paragraphs
    if (c !== "$") continue;
    if (text[j + 1] === "$") return -1;
    if (/\s/.test(text[j - 1]) || /\d/.test(text[j + 1] ?? "")) return -1;
    return j;
  }
  return -1;
}
