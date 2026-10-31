/** Clipboard plumbing for paste-ready artifacts.
 *
 * What lands in the clipboard has to survive the paste: a diagram arrives in
 * Slack as a picture, a table in Sheets as cells, code as exactly the
 * characters the agent wrote.
 *
 * 1. `navigator.clipboard` only exists in a secure context, and the server is
 *    plain http on :8787, so over the LAN there is no async clipboard (only
 *    `localhost` is exempt). Text falls back to a hidden textarea and
 *    `execCommand`; image copy reports itself unavailable so callers can offer
 *    a download instead.
 * 2. Rasterising an SVG goes through `<img>`, an isolated document: no page
 *    CSS, no page fonts and, in Chrome and Safari, no `<foreignObject>`. Hence
 *    Mermaid's `htmlLabels: false` (Artifacts.tsx), so labels are real `<text>`.
 */

/** Async clipboard, present only in a secure context. */
const asyncClipboard = typeof navigator !== "undefined" && navigator.clipboard ? navigator.clipboard : null;

/** Whether a real image can reach the clipboard. Firefox exposes
 * `ClipboardItem` but may refuse anything but text/plain from
 * `write()`; that failure is caught at call time, this only gates the UI. */
export const canCopyImage: boolean =
  !!asyncClipboard && typeof window !== "undefined" && typeof window.ClipboardItem === "function";

/** Last-resort text copy for non-secure contexts (LAN http). */
function legacyCopyText(text: string): boolean {
  const ta = document.createElement("textarea");
  ta.value = text;
  // Off-screen but still focusable: `display:none` would make the selection fail.
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:-9999px;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  ta.remove();
  return ok;
}

/** Copy plain text. Resolves false when every path is refused. */
export async function copyText(text: string): Promise<boolean> {
  if (asyncClipboard) {
    try {
      await asyncClipboard.writeText(text);
      return true;
    } catch {
      /* fall through to the legacy path */
    }
  }
  return legacyCopyText(text);
}

/**
 * Copy with both a rich and a plain flavour, so one paste satisfies both
 * kinds of destination: Docs/Confluence/Sheets take the `text/html` and
 * rebuild real formatting, while an editor or terminal takes the `text/plain`.
 */
export async function copyRich(html: string, plain: string): Promise<boolean> {
  if (asyncClipboard && typeof window.ClipboardItem === "function") {
    try {
      await asyncClipboard.write([
        new ClipboardItem({
          "text/html": new Blob([html], { type: "text/html" }),
          "text/plain": new Blob([plain], { type: "text/plain" }),
        }),
      ]);
      return true;
    } catch {
      /* fall back to plain text */
    }
  }
  return copyText(plain);
}

/** Copy a PNG blob as a real image. */
export async function copyImageBlob(blob: Blob): Promise<boolean> {
  if (!asyncClipboard || typeof window.ClipboardItem !== "function") return false;
  try {
    await asyncClipboard.write([new ClipboardItem({ [blob.type]: blob })]);
    return true;
  } catch {
    return false;
  }
}

export function download(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  // Revoke later: revoking synchronously can beat the download.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Font stack baked into rasterised SVGs. `inherit` resolves to the browser
 * default serif once the markup is loaded as an isolated image document, so
 * the stack has to be written out concretely. */
export const RASTER_FONT =
  'ui-sans-serif, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", "PingFang TC", "Noto Sans TC", sans-serif';

/**
 * Rasterise an on-screen `<svg>` to a PNG blob.
 *
 * `background` is opaque by default: a transparent diagram pasted into Slack's
 * dark theme or a Google Doc becomes dark-on-dark and unreadable, so the paper
 * plane travels with the picture.
 */
export async function svgToPngBlob(
  svg: SVGSVGElement,
  { scale = 2, background = "#ffffff" }: { scale?: number; background?: string } = {},
): Promise<Blob> {
  const clone = svg.cloneNode(true) as SVGSVGElement;

  // Mermaid sizes its output with `style="max-width:…"` and a viewBox rather
  // than concrete attributes; an <img> with no intrinsic size renders 0×0.
  const box = svg.viewBox.baseVal;
  const rect = svg.getBoundingClientRect();
  const width = Math.ceil(box?.width || rect.width || 800);
  const height = Math.ceil(box?.height || rect.height || 600);
  clone.setAttribute("width", String(width));
  clone.setAttribute("height", String(height));
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("xmlns:xlink", "http://www.w3.org/1999/xlink");
  clone.style.removeProperty("max-width");
  clone.style.fontFamily = RASTER_FONT;

  const source = new XMLSerializer().serializeToString(clone);
  // Inline data URL rather than a blob URL: Safari taints the canvas for
  // blob-sourced SVG images, which makes toBlob() throw a security error.
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(source)}`;

  const img = new Image();
  img.decoding = "sync";
  await new Promise<void>((resolve, reject) => {
    img.onload = () => resolve();
    img.onerror = () => reject(new Error("The diagram could not be rasterised."));
    img.src = url;
  });

  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, width * scale);
  canvas.height = Math.max(1, height * scale);
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("This browser has no 2D canvas.");
  if (background) {
    ctx.fillStyle = background;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (b) => (b ? resolve(b) : reject(new Error("The diagram could not be encoded as PNG."))),
      "image/png",
    );
  });
}

/** Fetch an `<img>` source and normalise it to a PNG blob the clipboard accepts. */
export async function imageElementToPngBlob(img: HTMLImageElement): Promise<Blob> {
  const canvas = document.createElement("canvas");
  canvas.width = img.naturalWidth || img.width;
  canvas.height = img.naturalHeight || img.height;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("This browser has no 2D canvas.");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(img, 0, 0);
  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (b) => (b ? resolve(b) : reject(new Error("The image could not be encoded as PNG."))),
      "image/png",
    );
  });
}

const CELL_ESCAPE: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;" };
const esc = (s: string) => s.replace(/[&<>]/g, (c) => CELL_ESCAPE[c]);

/**
 * A rendered `<table>` as both TSV and standalone HTML.
 *
 * TSV is what spreadsheets parse into cells, so tabs and newlines inside a
 * cell have to become spaces or the row count changes on paste. The HTML
 * carries its own inline borders because the destination has none of our CSS.
 */
export function tableToClipboard(table: HTMLTableElement): { html: string; plain: string } {
  const rows = Array.from(table.rows).map((row) =>
    Array.from(row.cells).map((cell) => ({
      text: (cell.innerText || cell.textContent || "").replace(/\s+/g, " ").trim(),
      head: cell.tagName === "TH",
    })),
  );
  const plain = rows.map((r) => r.map((c) => c.text).join("\t")).join("\n");
  const html =
    '<table style="border-collapse:collapse">' +
    rows
      .map(
        (r) =>
          "<tr>" +
          r
            .map(
              (c) =>
                `<${c.head ? "th" : "td"} style="border:1px solid #d2d4da;padding:4px 8px;text-align:left">` +
                esc(c.text) +
                `</${c.head ? "th" : "td"}>`,
            )
            .join("") +
          "</tr>",
      )
      .join("") +
    "</table>";
  return { html, plain };
}
