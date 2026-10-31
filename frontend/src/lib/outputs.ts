/**
 * Paths to agent outputs mentioned in text — a tool result ("Shipped ->
 * knowledge_base/outputs/reports/x.md", "完整產出：outputs/delegations/y.md") or
 * a reply. Each comes back relative to the outputs directory, which is how the
 * viewer and /context_api/outputs name it.
 */

const OUTPUT_PATH =
  /(?:knowledge_base\/)?outputs\/([^\s`"'()<>\[\]，。、；：）]+?\.(?:md|txt|json|html|csv|svg|drawio|png|jpe?g|gif|webp|m4a))(?![\w.])/gi;

export function outputPaths(text: string | undefined): string[] {
  if (!text) return [];
  const out: string[] = [];
  for (const m of text.matchAll(OUTPUT_PATH)) {
    if (!out.includes(m[1])) out.push(m[1]);
  }
  return out;
}

export const isImage = (path: string) => /\.(png|jpe?g|gif|webp|svg)$/i.test(path);
