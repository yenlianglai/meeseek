import { type ComponentPropsWithoutRef, memo, useEffect, useMemo, useSyncExternalStore } from "react";
import Markdown, { type Components, type Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { remarkBr } from "./breaks";
import { normalizeMath } from "./math";
import { currentPlugins, needPluginsFor, subscribePlugins } from "./mdPlugins";
import { extractText, MermaidBlock } from "../chat/Artifacts";

/** A ```mermaid block is drawn, wherever the markdown is shown — a note, an
 *  output, an Inbox reply; anything else stays a code block. */
function Pre(props: ComponentPropsWithoutRef<"pre">) {
  const child = Array.isArray(props.children) ? props.children[0] : props.children;
  const cp = child && typeof child === "object" && "props" in child
    ? (child as { props: { className?: string; children?: React.ReactNode } }).props
    : undefined;
  if (/language-mermaid/.test(cp?.className ?? "")) return <MermaidBlock code={extractText(cp?.children)} />;
  return <pre {...props} />;
}

// A module constant, so a re-render with the same text is a memo hit rather
// than a fresh parse through every plugin. The rehype side (KaTeX, highlight)
// arrives on demand: see mdPlugins.ts.
const REMARK: Options["remarkPlugins"] = [remarkGfm, remarkMath, remarkBr];
/** A link in a reply opens beside the console, not over it — leaving mid-stream
 *  would drop the turn on screen. The console's own links (#/…) stay put. */
function A({ node: _node, ...props }: ComponentPropsWithoutRef<"a"> & { node?: unknown }) {
  const away = !!props.href && !props.href.startsWith("#");
  return <a {...props} {...(away ? { target: "_blank", rel: "noopener noreferrer" } : {})} />;
}

const BASE: Components = { pre: Pre, a: A };

/** Every model-written markdown in the console renders here: GFM, `<br>` in
 * table cells, LaTeX through KaTeX, highlighted code, Mermaid drawn. A
 * caller's `components` (the chat's) override these; pass a module constant,
 * or the memo misses every render. */
export const Md = memo(function Md({ children, components }: { children: string; components?: Components }) {
  const merged = useMemo(() => (components ? { ...BASE, ...components } : BASE), [components]);
  const rehype = useSyncExternalStore(subscribePlugins, currentPlugins);
  useEffect(() => needPluginsFor(children), [children]);
  return (
    <Markdown remarkPlugins={REMARK} rehypePlugins={rehype} components={merged}>
      {normalizeMath(children)}
    </Markdown>
  );
});
