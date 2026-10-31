import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { contextApi } from "../lib/api";
import { copyText } from "../lib/clipboard";
import { isImage } from "../lib/outputs";
import { CopyButton } from "../ui/Copy";
import { Md } from "../markdown/Md";

const PAGE_SANDBOX = "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads";

/* An agent output, opened whole from the conversation that produced it. Outputs
   are not context — no agent reads them back — so they have no page of their
   own: a path in a tool result or a reply opens this viewer, read-only, with
   delete for one that is not worth keeping. */

const OpenOutput = createContext<(path: string) => void>(() => {});

/** Open an output (a path relative to the outputs directory) in the viewer. */
export const useOpenOutput = () => useContext(OpenOutput);

export function OutputProvider({ children }: { children: ReactNode }) {
  const [path, setPath] = useState<string | null>(null);
  const open = useCallback((p: string) => setPath(p), []);
  const close = useCallback(() => setPath(null), []);
  return (
    <OpenOutput.Provider value={open}>
      {children}
      {path && <OutputViewer path={path} onClose={close} />}
    </OpenOutput.Provider>
  );
}

/** "Open output →" for each output a text names. */
export function OutputLinks({ paths }: { paths: string[] }) {
  const open = useOpenOutput();
  if (paths.length === 0) return null;
  return (
    <>
      {paths.map((p) => (
        <button key={p} type="button" className="tool-link" title={`outputs/${p}`}
                onClick={(e) => { e.preventDefault(); e.stopPropagation(); open(p); }}>
          {paths.length === 1 ? "Open output →" : `${p.split("/").pop()} →`}
        </button>
      ))}
    </>
  );
}

function OutputViewer({ path, onClose }: { path: string; onClose: () => void }) {
  const [file, setFile] = useState<{ text: string; etag: string } | null>(null);
  const [error, setError] = useState("");
  const image = isImage(path) && !path.endsWith(".svg");
  const audio = /\.m4a$/i.test(path);   // a meeting's recording
  const page = /\.html?$/i.test(path);
  const [source, setSource] = useState(false);
  const url = `/outputs/${path.split("/").map(encodeURIComponent).join("/")}`;

  useEffect(() => {
    if (image || audio) return;
    contextApi<{ text: string; etag: string }>(`/outputs/file?path=${encodeURIComponent(path)}`)
      .then(setFile)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [path, image, audio]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function remove() {
    if (!confirm(`Delete outputs/${path}? This cannot be undone.`)) return;
    try {
      const etag = file?.etag
        ?? (await contextApi<{ path: string; etag: string }[]>("/outputs")).find((f) => f.path === path)?.etag;
      if (!etag) throw new Error("file not found");
      await contextApi(`/outputs/file?path=${encodeURIComponent(path)}&etag=${encodeURIComponent(etag)}`, "DELETE");
      onClose();
    } catch (e) {
      setError(`Could not delete: ${e instanceof Error ? e.message : e}`);
    }
  }

  return (
    <div className="viewer-scrim" onClick={onClose}>
      <div className="viewer" role="dialog" aria-label={`outputs/${path}`} onClick={(e) => e.stopPropagation()}>
        <div className="ctx-editor-head">
          <code>outputs/{path}</code>
          <span className="ctx-spacer" />
          {page && (
            <button className="btn-ghost small" onClick={() => setSource((s) => !s)}>{source ? "Page" : "Source"}</button>
          )}
          {file && <CopyButton title="Copy the file" onCopy={() => copyText(file.text)} />}
          <a className="btn-ghost small" href={url} target="_blank" rel="noreferrer">Open ↗</a>
          <button className="btn-ghost small danger" onClick={remove}>Delete</button>
          <button className="btn-ghost small" onClick={onClose} aria-label="Close">Close</button>
        </div>
        {error && <div className="notice">{error}</div>}
        <div className="viewer-body">
          {image ? (
            <img className="ctx-image" src={url} alt={path} />
          ) : audio ? (
            <audio className="viewer-audio" controls preload="metadata" src={url} />
          ) : page && !source ? (
            <iframe className="viewer-page" title={path} src={url} sandbox={PAGE_SANDBOX} referrerPolicy="no-referrer" />
          ) : !file ? (
            !error && <div className="skeleton" />
          ) : /\.(md|txt)$/i.test(path) ? (
            <div className="md"><Md>{file.text}</Md></div>
          ) : (
            <pre>{file.text}</pre>
          )}
        </div>
      </div>
    </div>
  );
}
