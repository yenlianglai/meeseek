import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, contextApi } from "../lib/api";
import { useNavigate } from "../lib/nav";
import type { ContextKind } from "../lib/route";
import { FactsPanel, RulesPanel } from "./Learning";
import { Md } from "../markdown/Md";
import { character } from "../ui/character";

/* Everything the agents know that you may change, one kind per tab. Rules and
   facts are entries, written through the same checks an agent's write goes
   through; the rest are files the agents read directly, edited here with a
   guard against overwriting an edit made elsewhere (the IDE, a sync, an
   agent). Drive sources and SKILL.md are not here: Drive and git own them.
   Agent outputs are not context either; they open from their conversation
   (OutputViewer). */

const KINDS: { kind: ContextKind; label: string; note: string }[] = [
  { kind: "rules", label: "Rules", note: "" },
  { kind: "facts", label: "Facts", note: "" },
  { kind: "pinned", label: "Pinned", note: "Read in full by every agent, every turn." },
  { kind: "notes", label: "Notes", note: "" },
  { kind: "meetings", label: "Meetings", note: "" },
];

interface FileEntry {
  path: string;
  size: number;
  mtime: number;
  etag: string;
  text: boolean;
  url: string;
}

interface Open {
  path: string;
  text: string;   // as saved
  draft: string;  // as edited
  etag: string;
}

const NEW_FILE: Record<string, string> = {
  pinned: "new-note.md", notes: "new-note.md", meetings: "new-meeting.md",
};

function size(n: number): string {
  return n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
}

function ago(epoch: number): string {
  const s = Date.now() / 1000 - epoch;
  if (s < 3600) return `${Math.max(1, Math.floor(s / 60))}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(epoch * 1000).toLocaleDateString();
}

function FilesPanel({ kind, path }: { kind: ContextKind; path?: string }) {
  const navigate = useNavigate();
  const [files, setFiles] = useState<FileEntry[] | null>(null);
  const [open, setOpen] = useState<Open | null>(null);
  const [preview, setPreview] = useState(true);
  const [filter, setFilter] = useState("");
  const [creating, setCreating] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setFiles(await contextApi<FileEntry[]>(`/${kind}`).catch(() => []));
  }, [kind]);

  useEffect(() => {
    setFiles(null);
    setOpen(null);
    setFilter("");
    setCreating(null);
    setNote("");
    refresh();
  }, [kind, refresh]);

  const entry = files?.find((f) => f.path === path);
  const dirty = !!open && open.draft !== open.text;

  // Load the file the route names.
  useEffect(() => {
    if (!path || !entry?.text || open?.path === path) return;
    setConflict(false);
    contextApi<{ path: string; text: string; etag: string }>(`/${kind}/file?path=${encodeURIComponent(path)}`)
      .then((f) => {
        setOpen({ path: f.path, text: f.text, draft: f.text, etag: f.etag });
        setPreview(true);
      })
      .catch((e) => setNote(`Could not open: ${e instanceof Error ? e.message : e}`));
  }, [kind, path, entry?.text, open?.path]);

  function select(p?: string) {
    if (dirty && !confirm("Discard unsaved changes?")) return;
    setOpen(null);
    setConflict(false);
    navigate({ view: "context", kind, path: p });
  }

  async function save() {
    if (!open) return;
    setBusy(true);
    try {
      const out = await contextApi<{ etag: string }>(`/${kind}/file`, "PUT",
        { path: open.path, text: open.draft, etag: open.etag });
      setOpen({ ...open, text: open.draft, etag: out.etag });
      setNote(`Saved ${open.path}`);
      setConflict(false);
      await refresh();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setConflict(true);
      else setNote(`Could not save: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  async function reload() {
    if (!open) return;
    const f = await contextApi<{ path: string; text: string; etag: string }>(
      `/${kind}/file?path=${encodeURIComponent(open.path)}`);
    setOpen({ path: f.path, text: f.text, draft: f.text, etag: f.etag });
    setConflict(false);
    await refresh();
  }

  async function remove(f: FileEntry) {
    if (!confirm(`Delete ${f.path}? This cannot be undone.`)) return;
    setBusy(true);
    try {
      await contextApi(`/${kind}/file?path=${encodeURIComponent(f.path)}&etag=${encodeURIComponent(open?.path === f.path ? open.etag : f.etag)}`, "DELETE");
      setNote(`Deleted ${f.path}`);
      setOpen(null);
      navigate({ view: "context", kind });
      await refresh();
    } catch (e) {
      setNote(`Could not delete: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    const p = (creating ?? "").trim();
    if (!p) return;
    setBusy(true);
    try {
      const title = p.split("/").pop()!.replace(/\.[^.]+$/, "");
      await contextApi(`/${kind}/file`, "PUT", { path: p, text: `# ${title}\n\n`, etag: null });
      setCreating(null);
      await refresh();
      navigate({ view: "context", kind, path: p });
      setPreview(false);
    } catch (e) {
      setNote(`Could not create: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  const shown = useMemo(
    () => (files ?? []).filter((f) => !filter || f.path.toLowerCase().includes(filter.toLowerCase())),
    [files, filter]);
  const total = (files ?? []).reduce((n, f) => n + f.size, 0);

  return (
    <>
      {note && <div className="notice">{note}</div>}
      <div className="ctx-files">
        <div className="ctx-list">
          <div className="ctx-list-head">
            <input value={filter} placeholder={`Filter ${files?.length ?? 0} files`} onChange={(e) => setFilter(e.target.value)} />
            <button className="btn-ghost small" onClick={() => setCreating(NEW_FILE[kind] ?? "new.md")}>New</button>
          </div>
          {creating !== null && (
            <div className="ctx-form inline">
              <input value={creating} autoFocus onChange={(e) => setCreating(e.target.value)}
                     onKeyDown={(e) => { if (e.key === "Enter") create(); if (e.key === "Escape") setCreating(null); }} />
              <button className="btn-primary small" disabled={busy} onClick={create}>Create</button>
            </div>
          )}
          {files === null ? (
            <div className="skeleton" />
          ) : shown.length === 0 ? (
            <p className="usage-note">No files.</p>
          ) : (
            <ul>
              {shown.map((f) => (
                <li key={f.path}>
                  <button className={`ctx-file ${f.path === path ? "active" : ""}`} onClick={() => select(f.path)} title={f.path}>
                    <span className="ctx-file-name">{f.path}</span>
                    <span className="ctx-file-meta">{size(f.size)} · {ago(f.mtime)}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {kind === "pinned" && files && <p className="usage-note">{size(total)} in every turn</p>}
        </div>

        <div className="ctx-editor">
          {!path || !entry ? (
            <p className="usage-note">{path ? "File not found." : "Select a file."}</p>
          ) : !entry.text ? (
            <>
              <div className="ctx-editor-head">
                <code>{entry.path}</code>
                <span className="ctx-spacer" />
                {entry.url && <a className="btn-ghost small" href={entry.url} target="_blank" rel="noreferrer">Open ↗</a>}
                <button className="btn-ghost small danger" disabled={busy} onClick={() => remove(entry)}>Delete</button>
              </div>
              {entry.url && /\.(png|jpe?g|gif|webp)$/i.test(entry.path) && (
                <img className="ctx-image" src={entry.url} alt={entry.path} />
              )}
            </>
          ) : !open ? (
            <div className="skeleton" />
          ) : (
            <>
              <div className="ctx-editor-head">
                <code>{open.path}</code>
                {dirty && <span className="ctx-dirty">Unsaved</span>}
                <span className="ctx-spacer" />
                <div className="seg" role="group" aria-label="Mode">
                  <button aria-pressed={preview} onClick={() => setPreview(true)}>Preview</button>
                  <button aria-pressed={!preview} onClick={() => setPreview(false)}>Edit</button>
                </div>
                <button className="btn-primary small" disabled={busy || !dirty} onClick={save}>Save</button>
                <button className="btn-ghost small danger" disabled={busy} onClick={() => remove(entry)}>Delete</button>
              </div>
              {conflict && (
                <div className="notice">
                  Not saved: this file changed after you opened it (the IDE, a sync or an agent).
                  <button className="btn-ghost small" onClick={reload}>Load the latest (discard mine)</button>
                </div>
              )}
              {preview ? (
                <div className="md ctx-preview">
                  {/\.(md|txt)$/i.test(open.path) ? <Md>{open.draft}</Md> : <pre>{open.draft}</pre>}
                </div>
              ) : (
                <textarea className="ctx-textarea" value={open.draft} spellCheck={false}
                          onChange={(e) => setOpen({ ...open, draft: e.target.value })}
                          onKeyDown={(e) => {
                            if ((e.metaKey || e.ctrlKey) && e.key === "s") {
                              e.preventDefault();
                              if (dirty) save();
                            }
                          }} />
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}

export function Context({ kind = "rules", path }: { kind?: ContextKind; path?: string }) {
  const navigate = useNavigate();
  const meta = KINDS.find((k) => k.kind === kind) ?? KINDS[0];
  const files = kind !== "rules" && kind !== "facts";
  return (
    <div className="usage-scroll">
      <div className={`usage context ${files ? "wide" : ""}`}>
        {character.Page && <character.Page page="context" say={`Ooh, ${meta.label.toLowerCase()}!`} />}
        <div className="seg ctx-kinds" role="tablist" aria-label="Context">
          {KINDS.map((k) => (
            <button key={k.kind} role="tab" aria-pressed={k.kind === kind}
                    onClick={() => navigate({ view: "context", kind: k.kind })}>
              {k.label}
            </button>
          ))}
        </div>
        {meta.note && <p className="usage-note">{meta.note}</p>}
        {kind === "rules" ? <RulesPanel /> : kind === "facts" ? <FactsPanel /> : <FilesPanel kind={kind} path={path} />}
      </div>
    </div>
  );
}
