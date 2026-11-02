/** Where the console is, as a URL hash — the one source of "where am I".
 *
 *  Every click that changes what is on screen is a navigation to a Route, and
 *  the screen is drawn from the Route; back, forward, reload and a pasted link
 *  all land in the same place. Each object has one home: a session is shown in
 *  Chat (its Trace beside it), an Inbox item in Inbox.
 *
 *    #/chat/<session>?as=<owner>     #/trace/<session>?as=<owner>
 *    #/inbox/<item>    #/work/<id>    #/usage?days=30    #/context/<kind>/<path>
 *
 *  `as` names a session owner other than the console's own (an MCP caller). */

export const CONSOLE_OWNER = "webui";

export type Route =
  | { view: "chat" | "trace"; session?: string; owner?: string }
  | { view: "inbox"; item?: string }
  | { view: "work"; id?: string }
  | { view: "usage"; days?: number }
  | { view: "context"; kind?: ContextKind; path?: string };

/** What the Context page edits: entries (rules, facts) and files. */
export type ContextKind = "rules" | "facts" | "pinned" | "notes" | "meetings";
const CONTEXT_KINDS = new Set<string>(["rules", "facts", "pinned", "notes", "meetings"]);

export type View = Route["view"];

/** Views that show one session; the rest are team-wide. */
export function isSessionView(r: Route): r is Extract<Route, { view: "chat" | "trace" }> {
  return r.view === "chat" || r.view === "trace";
}

export function parseRoute(hash: string): Route {
  const [path, query = ""] = hash.replace(/^#\/?/, "").split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const params = new URLSearchParams(query);
  const view = parts[0];
  if (view === "chat" || view === "trace") {
    const owner = params.get("as") || undefined;
    return { view, session: parts[1], owner: owner === CONSOLE_OWNER ? undefined : owner };
  }
  if (view === "inbox") return { view, item: parts[1] };
  if (view === "work") return { view, id: parts[1] };
  if (view === "usage") {
    const days = Number(params.get("days"));
    return { view, days: Number.isFinite(days) && days > 0 ? days : undefined };
  }
  // Old #/learning links open the rules.
  if (view === "learning") return { view: "context", kind: "rules" };
  if (view === "context") {
    const kind = CONTEXT_KINDS.has(parts[1]) ? (parts[1] as ContextKind) : undefined;
    return { view, kind, path: kind ? parts[2] : undefined };
  }
  return { view: "chat" };
}

export function formatRoute(r: Route): string {
  const seg = (s?: string) => (s ? `/${encodeURIComponent(s)}` : "");
  switch (r.view) {
    case "chat":
    case "trace":
      return `#/${r.view}${seg(r.session)}${r.owner && r.owner !== CONSOLE_OWNER ? `?as=${encodeURIComponent(r.owner)}` : ""}`;
    case "inbox":
      return `#/inbox${seg(r.item)}`;
    case "work":
      return `#/work${seg(r.id)}`;
    case "usage":
      return `#/usage${r.days ? `?days=${r.days}` : ""}`;
    case "context":
      return `#/context${seg(r.kind)}${r.kind ? seg(r.path) : ""}`;
  }
}
