import { useState } from "react";
import { useOpenOutput } from "./OutputViewer";

/* A web page an agent made (an HTML file under outputs/), live in the
   conversation. It runs sandboxed twice over: this frame gives it an opaque
   origin, and the server sends every page with a CSP sandbox and no network
   (server.py: PAGE_CSP), so it is just as shut in when opened in a tab. */

export const isPage = (path: string) => /\.html?$/i.test(path);

const SANDBOX = "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads";

export function pageUrl(path: string): string {
  return `/outputs/${path.split("/").map(encodeURIComponent).join("/")}`;
}

export function PageFrame({ path, height = 520 }: { path: string; height?: number }) {
  const open = useOpenOutput();
  const [full, setFull] = useState(false);
  const url = pageUrl(path);
  const name = path.split("/").pop() ?? path;
  return (
    <div className={"page-frame" + (full ? " full" : "")}>
      <div className="page-frame-bar">
        <span className="page-frame-name" title={`outputs/${path}`}>{name}</span>
        <span className="page-frame-actions">
          <button type="button" className="btn-ghost small" onClick={() => open(path)}>Source</button>
          <a className="btn-ghost small" href={url} target="_blank" rel="noreferrer">Open ↗</a>
          <button type="button" className="btn-ghost small" onClick={() => setFull((f) => !f)}>
            {full ? "Close" : "Full screen"}
          </button>
        </span>
      </div>
      <iframe title={name} src={url} sandbox={SANDBOX} loading="lazy" referrerPolicy="no-referrer"
              style={full ? undefined : { height }} />
    </div>
  );
}
