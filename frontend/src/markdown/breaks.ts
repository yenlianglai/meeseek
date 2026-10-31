import type { Nodes, Parents } from "mdast";

const BR = /^<br\s*\/?>$/i;

/**
 * remark plugin: `<br>` becomes a line break. Models write it inside table
 * cells, where markdown has no other way to break a line. Raw HTML stays off
 * otherwise, so this is the only tag that renders.
 */
export function remarkBr() {
  return (tree: Nodes) => {
    walk(tree);
  };
}

function walk(node: Nodes) {
  if (!("children" in node)) return;
  const parent = node as Parents;
  parent.children = parent.children.map((child) =>
    child.type === "html" && BR.test(child.value.trim()) ? { type: "break" } : child,
  ) as Parents["children"];
  parent.children.forEach(walk);
}
