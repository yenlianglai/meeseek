import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import {
  canCopyImage,
  copyText,
  copyImageBlob,
  download,
  imageElementToPngBlob,
  RASTER_FONT,
  svgToPngBlob,
} from "../lib/clipboard";
import { ArtifactBar, CopyButton } from "../ui/Copy";
import { IconDownload, IconImage } from "../ui/Icons";

/** Recursively pull plain text out of a react-markdown code element. */
export function extractText(node: ReactNode): string {
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (node && typeof node === "object" && "props" in node) {
    return extractText((node as { props: { children?: ReactNode } }).props.children);
  }
  return "";
}

/** Quote bare `subgraph` titles that contain parser-breaking characters.
 *
 * Models routinely emit `subgraph DONE / Code Review (8)`, which mermaid
 * rejects. `subgraph id[Label]` and `subgraph "Label"` are left alone. Used
 * only as a retry, so a diagram that parses is never rewritten. */
export function repairMermaid(code: string): string {
  return code
    .split("\n")
    .map((line) => {
      const m = /^(\s*subgraph\s+)(.+?)\s*$/.exec(line);
      if (!m) return line;
      const title = m[2];
      const alreadyOk = /^"/.test(title) || /^[\w-]+\[/.test(title);
      if (alreadyOk || !/[()/,:[\]{}]/.test(title)) return line;
      return `${m[1]}"${title.replace(/"/g, "'")}"`;
    })
    .join("\n");
}

/**
 * Point an agent-written image path at the route that actually serves it.
 *
 * Answers embed the `knowledge_base/outputs/…` path the rendering tools
 * returned (sometimes made absolute); resolved against `/ui/` it would 404.
 * The server serves that directory at `/outputs`.
 */
export function resolveOutputSrc(src?: string): string | undefined {
  if (!src) return src;
  // A src the browser can already fetch is left alone; a file:// one is not one
  // of those, and it carries the same path, so it gets rewritten too.
  if (/^(https?|data|blob):/i.test(src)) return src;
  const at = src.indexOf("knowledge_base/outputs/");
  if (at < 0) return src;
  return "/outputs/" + src.slice(at + "knowledge_base/outputs/".length);
}

/** A short, human filename for a downloaded artifact. */
function stamp(): string {
  return new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-");
}

/** Mermaid diagrams, rendered client-side; the library loads lazily on the
 * first diagram.
 *
 * While streaming, this receives ever-longer PARTIAL diagrams that cannot
 * parse. Each attempt clears the previous verdict, so an early failure never
 * sticks, and the last good SVG stays on screen instead of flashing an error.
 *
 * `htmlLabels: false` is load-bearing: labels become SVG `<text>` rather than
 * `<foreignObject>`, which Chrome and Safari drop when the SVG is rasterised
 * for copying — the pasted picture would have no labels. */
export function MermaidBlock({ code }: { code: string }) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  const [svg, setSvg] = useState("");
  const [error, setError] = useState("");
  const attempt = useRef(0);
  const hostRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    const seq = ++attempt.current;
    // A streaming fence changes on every token and most of its partials do not
    // parse: draw once the source has held still for a moment. The first draw
    // (a finished message, a reload) does not wait.
    const wait = seq === 1 ? 0 : 300;
    const timer = window.setTimeout(() => void (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({
          startOnLoad: false,
          theme: "neutral",
          fontFamily: RASTER_FONT,
          htmlLabels: false,
          flowchart: { htmlLabels: false },
        });
        // Unique id per attempt: mermaid injects a temp DOM node keyed by id,
        // and reusing one across attempts (or React's double-invoked effect)
        // collides.
        let rendered;
        try {
          rendered = await mermaid.render(`mm${uid}s${seq}`, code);
        } catch (first) {
          // Retry once with mechanical syntax repairs (unquoted subgraph
          // titles). If that still fails, report the ORIGINAL error — the
          // repaired source is not what the author wrote.
          document.getElementById(`dmm${uid}s${seq}`)?.remove();
          const patched = repairMermaid(code);
          if (patched === code) throw first;
          rendered = await mermaid.render(`mm${uid}s${seq}r`, patched).catch(() => {
            throw first;
          });
        }
        if (!alive || seq !== attempt.current) return; // a newer chunk won
        setSvg(rendered.svg);
        setError("");
      } catch (e) {
        if (!alive || seq !== attempt.current) return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        // A failed parse leaves mermaid's error node behind; drop it so broken
        // partials don't pile up invisible "syntax error" SVGs in the body.
        document.getElementById(`dmm${uid}s${seq}`)?.remove();
        document.getElementById(`dmm${uid}s${seq}r`)?.remove();
      }
    })(), wait);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [code, uid]);

  function liveSvg(): SVGSVGElement {
    const el = hostRef.current?.querySelector("svg");
    if (!el) throw new Error("The diagram is not on screen yet.");
    return el as SVGSVGElement;
  }

  if (svg) {
    return (
      <div className="artifact-block">
        <ArtifactBar label="diagram">
          {/* Image first: a diagram is almost always wanted as a picture, and
              a ticket or a chat message cannot render mermaid source. */}
          {canCopyImage ? (
            <CopyButton
              label="Copy image"
              title="Copy the diagram as a PNG image, ready to paste into chat, a ticket or a doc"
              icon={<IconImage />}
              onCopy={async () => copyImageBlob(await svgToPngBlob(liveSvg()))}
            />
          ) : (
            <CopyButton
              label="Save PNG"
              done="Saved"
              title="Download the diagram as a PNG image (this browser blocks copying images)"
              icon={<IconDownload />}
              onCopy={async () => {
                download(await svgToPngBlob(liveSvg()), `diagram-${stamp()}.png`);
                return true;
              }}
            />
          )}
          <CopyButton
            label="Source"
            title="Copy the mermaid source"
            onCopy={() => copyText(code)}
          />
          <CopyButton
            label="SVG"
            done="Saved"
            title="Download the diagram as a scalable SVG file"
            icon={<IconDownload />}
            onCopy={() => {
              download(
                new Blob([liveSvg().outerHTML], { type: "image/svg+xml" }),
                `diagram-${stamp()}.svg`,
              );
              return true;
            }}
          />
        </ArtifactBar>
        <div className="mermaid-svg" ref={hostRef} dangerouslySetInnerHTML={{ __html: svg }} />
      </div>
    );
  }
  if (error) {
    return (
      <div className="artifact-block">
        <ArtifactBar label={`mermaid — ${error.split("\n")[0].slice(0, 90)}`}>
          <CopyButton label="Source" title="Copy the mermaid source" onCopy={() => copyText(code)} />
        </ArtifactBar>
        <pre>
          <code>{code}</code>
        </pre>
      </div>
    );
  }
  return (
    <div className="artifact-block loading" aria-live="polite">
      Rendering diagram…
    </div>
  );
}

/** HTML artifacts: source by default, opt-in preview inside a sandboxed
 * iframe (scripts allowed, same-origin denied — it cannot touch the console
 * or the server's API). */
export function HtmlBlock({ code }: { code: string }) {
  const [preview, setPreview] = useState(false);
  return (
    <div className="artifact-block">
      <ArtifactBar label="html">
        <div className="seg mini" role="group" aria-label="HTML view">
          <button type="button" aria-pressed={!preview} onClick={() => setPreview(false)}>
            Source
          </button>
          <button type="button" aria-pressed={preview} onClick={() => setPreview(true)}>
            Preview
          </button>
        </div>
        <CopyButton label="Copy" title="Copy the HTML source" onCopy={() => copyText(code)} />
        <CopyButton
          done="Saved"
          title="Download as a standalone .html file"
          icon={<IconDownload />}
          onCopy={() => {
            download(new Blob([code], { type: "text/html" }), `artifact-${stamp()}.html`);
            return true;
          }}
        />
      </ArtifactBar>
      {preview ? (
        <iframe
          className="html-preview"
          sandbox="allow-scripts"
          srcDoc={code}
          title="HTML artifact preview"
        />
      ) : (
        <pre>
          <code>{code}</code>
        </pre>
      )}
    </div>
  );
}

/**
 * Any other fenced block. The copy takes the source verbatim rather than the
 * highlighted rendering, whose spans can mangle a hand-made selection.
 */
export function CodeBlock({ code, lang, children }: { code: string; lang?: string; children: ReactNode }) {
  return (
    <div className="artifact-block code">
      <ArtifactBar label={lang || "text"}>
        <CopyButton label="Copy" title={`Copy the ${lang || "code"} block`} onCopy={() => copyText(code)} />
      </ArtifactBar>
      {children}
    </div>
  );
}

/**
 * An image in agent output, copyable as real PNG bytes (through a canvas)
 * rather than a URL that only resolves against this server.
 */
export function CopyableImage(props: { src?: string; alt?: string; title?: string }) {
  const ref = useRef<HTMLImageElement>(null);
  const [broken, setBroken] = useState(false);
  const src = resolveOutputSrc(props.src);

  function liveImg(): HTMLImageElement {
    const el = ref.current;
    if (!el || !el.complete || !el.naturalWidth) throw new Error("The image has not finished loading.");
    return el;
  }

  if (broken) {
    return (
      <span className="img-broken">
        {props.alt || "Image"} — this file could not be loaded from the server.
      </span>
    );
  }
  return (
    <span className="img-block">
      <img
        ref={ref}
        src={src}
        alt={props.alt ?? ""}
        title={props.title}
        loading="lazy"
        // Same-origin under /ui; anonymous CORS keeps the canvas untainted if a
        // tool ever hands back an image from elsewhere.
        crossOrigin="anonymous"
        onError={() => setBroken(true)}
      />
      <span className="img-actions">
        {canCopyImage && (
          <CopyButton
            title="Copy this image, ready to paste into chat, a ticket or a doc"
            icon={<IconImage />}
            onCopy={async () => copyImageBlob(await imageElementToPngBlob(liveImg()))}
          />
        )}
        <CopyButton
          done="Saved"
          title="Download this image"
          icon={<IconDownload />}
          onCopy={async () => {
            download(await imageElementToPngBlob(liveImg()), `image-${stamp()}.png`);
            return true;
          }}
        />
      </span>
    </span>
  );
}
