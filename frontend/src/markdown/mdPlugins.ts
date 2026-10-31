/** The heavy markdown plugins, loaded when first needed rather than in the
 *  first page load: KaTeX (with its stylesheet) and highlight.js are most of
 *  the console's JavaScript, and most messages hold neither math nor code.
 *
 *  Until one arrives, math and code show as their source; the first message
 *  that needs it starts the load, and every Md re-renders once it lands. Both
 *  are also fetched when the browser is next idle after the first paint, so in
 *  practice they are there before anyone asks. */

import type { Options } from "react-markdown";

type Plugin = NonNullable<Options["rehypePlugins"]>[number];
type Name = "katex" | "highlight";

const LOADERS: Record<Name, () => Promise<Plugin>> = {
  katex: async () => {
    const m = await import("./katexPlugin");
    return [m.default, { throwOnError: false, strict: "ignore" }];
  },
  highlight: async () => [(await import("rehype-highlight")).default, { ignoreMissing: true }],
};

const loaded: Partial<Record<Name, Plugin>> = {};
const loading = new Set<Name>();
const listeners = new Set<() => void>();
// Rebuilt only when a plugin lands, so Md's memo sees the same list until then.
// KaTeX runs before highlight, as it always has.
let plugins: Plugin[] = [];

export function loadPlugin(name: Name): void {
  if (loaded[name] || loading.has(name)) return;
  loading.add(name);
  LOADERS[name]()
    .then((p) => {
      loaded[name] = p;
      plugins = (["katex", "highlight"] as const).flatMap((n) => (loaded[n] ? [loaded[n]!] : []));
      listeners.forEach((l) => l());
    })
    // Offline or a stale chunk after a deploy: the source stays readable, and
    // the next message that needs the plugin tries again.
    .catch(() => {})
    .finally(() => loading.delete(name));
}

export function subscribePlugins(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The rehype plugins loaded so far, for `useSyncExternalStore`. */
export const currentPlugins = (): Plugin[] => plugins;

const FENCE = /^ {0,3}(`{3,}|~{3,})/m;

/** Start loading whatever this text needs that is not here yet. */
export function needPluginsFor(text: string): void {
  if (text.includes("$") || text.includes("\\[") || text.includes("\\(")) loadPlugin("katex");
  if (FENCE.test(text)) loadPlugin("highlight");
}

/** After the first paint, when the browser has nothing better to do. */
export function preloadPlugins(): void {
  const go = () => { loadPlugin("katex"); loadPlugin("highlight"); };
  if ("requestIdleCallback" in window) window.requestIdleCallback(go, { timeout: 4000 });
  else setTimeout(go, 1500);
}
