"""Shared Google Drive and Sheets API utilities."""

import io
import re
import time
import zipfile
from xml.etree import ElementTree

from googleapiclient.errors import HttpError


def build_service(api: str, version: str, creds, timeout: int = 180):
    """googleapiclient build() with a longer per-request socket timeout.

    The library default (60s) is too tight for exporting large Docs — each
    retry hits the same wall, surfacing as 'The read operation timed out'.
    Returned services are NOT thread-safe; build one per thread.
    """
    import google_auth_httplib2
    import httplib2
    from googleapiclient.discovery import build

    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))
    return build(api, version, http=http)


def api_call_with_retry(fn, max_retries=5):
    """Execute a Google API call with exponential backoff on 429/5xx/timeout errors."""
    for attempt in range(max_retries):
        try:
            return fn()
        except HttpError as e:
            if e.resp.status in (429, 500, 503) and attempt < max_retries - 1:
                wait = 2**attempt * 10
                print(f"    ⏳ Rate limited, waiting {wait}s ({attempt + 1}/{max_retries})...")
                time.sleep(wait)
            else:
                raise
        except (TimeoutError, OSError):
            if attempt < max_retries - 1:
                wait = 2**attempt * 5
                print(f"    ⏳ Timeout, retrying in {wait}s ({attempt + 1}/{max_retries})...")
                time.sleep(wait)
            else:
                raise


def sanitize_filename(name: str) -> str:
    """Remove characters unsuitable for filenames."""
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def list_folder_files(
    drive_service, folder_id: str, unresolved_shortcuts: list[dict] | None = None
) -> list[dict]:
    """Recursively list all non-folder files under a Drive folder.

    Works across both "My Drive" and Shared Drives (supportsAllDrives /
    includeItemsFromAllDrives) — without those flags the API only sees My
    Drive, so Shared-Drive folders list empty and Shared-Drive file IDs 404.

    Shortcuts whose target can't be fetched (usually: target not shared with
    your account) are appended to `unresolved_shortcuts` if given, so
    the caller can report them once instead of spamming per-sync logs.
    """
    files: list[dict] = []
    query = f"'{folder_id}' in parents and trashed = false"
    page_token = None

    while True:
        resp = drive_service.files().list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, shortcutDetails)",
            pageSize=100,
            pageToken=page_token,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()

        for item in resp.get("files", []):
            mime = item["mimeType"]
            if mime == "application/vnd.google-apps.folder":
                files.extend(list_folder_files(drive_service, item["id"], unresolved_shortcuts))
            elif mime == "application/vnd.google-apps.shortcut":
                target_id = item.get("shortcutDetails", {}).get("targetId")
                if target_id:
                    try:
                        target = drive_service.files().get(
                            fileId=target_id,
                            fields="id, name, mimeType, modifiedTime",
                            supportsAllDrives=True,
                        ).execute()
                    except HttpError as e:
                        if unresolved_shortcuts is not None:
                            unresolved_shortcuts.append({
                                "name": item["name"],
                                "target_id": target_id,
                                "reason": str(e.reason),
                            })
                        else:
                            print(f"  [skip] Cannot resolve shortcut '{item['name']}': {e.reason}")
                        continue
                    if target["mimeType"] == "application/vnd.google-apps.folder":
                        files.extend(list_folder_files(drive_service, target["id"], unresolved_shortcuts))
                    else:
                        files.append(target)
            else:
                files.append(item)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return files


def export_google_doc(drive_service, file_id: str, max_retries: int = 5) -> str:
    """Export a Google Doc as plain text."""
    content = api_call_with_retry(
        lambda: drive_service.files()
        .export(fileId=file_id, mimeType="text/plain")
        .execute(),
        max_retries=max_retries,
    )
    return content.decode("utf-8") if isinstance(content, bytes) else content



def _extract_text_from_elements(elements: list) -> str:
    """Recursively extract plain text from Docs API structural elements."""
    text_parts = []
    for element in elements:
        if "paragraph" in element:
            for run in element["paragraph"].get("elements", []):
                if "textRun" in run:
                    text_parts.append(run["textRun"]["content"])
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                row_cells = []
                for cell in row.get("tableCells", []):
                    cell_text = _extract_text_from_elements(cell.get("content", [])).strip()
                    row_cells.append(cell_text)
                text_parts.append(" | ".join(row_cells) + "\n")
        elif "sectionBreak" in element:
            text_parts.append("\n")
    return "".join(text_parts)


def google_doc_to_text_via_docs_api(docs_service, file_id: str) -> str:
    """Extract a Google Doc's text via the Docs API (documents.get).

    Fallback for docs the Drive export API can't handle — export has a hard
    size limit and returns 500 Internal Error on very large docs. Handles
    tabbed documents (each tab becomes a `## <title>` section).
    """
    # NOTE: no `fields` filter — on very large docs Google's server-side
    # field filtering is pathologically slow (observed >6min vs ~1min for
    # the unfiltered read). Retry absorbs the intermittent 503s instead.
    doc = api_call_with_retry(
        lambda: docs_service.documents()
        .get(documentId=file_id, includeTabsContent=True)
        .execute()
    )
    tabs = doc.get("tabs")
    if not tabs:
        return _extract_text_from_elements(doc.get("body", {}).get("content", []))

    parts = []
    for tab in tabs:
        title = tab.get("tabProperties", {}).get("title", "")
        body = tab.get("documentTab", {}).get("body", {})
        text = _extract_text_from_elements(body.get("content", []))
        parts.append(f"## {title}\n\n{text}" if title else text)
    return "\n\n".join(parts)


def export_spreadsheet_as_md(sheets_service, spreadsheet_id: str) -> str:
    """Fetch all sheets from a spreadsheet and render as Markdown tables."""
    spreadsheet = api_call_with_retry(
        lambda: sheets_service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, includeGridData=False)
        .execute()
    )

    parts: list[str] = []
    sheet_titles = [s["properties"]["title"] for s in spreadsheet["sheets"]]
    links = _sheet_hyperlinks(sheets_service, spreadsheet_id)

    for title in sheet_titles:
        result = api_call_with_retry(
            lambda t=title: sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"'{t}'")
            .execute()
        )
        rows = result.get("values", [])
        if not rows:
            continue

        parts.append(f"## {title}\n")
        max_cols = max(len(r) for r in rows)
        normalized = [r + [""] * (max_cols - len(r)) for r in rows]
        sheet_links = links.get(title, {})
        if sheet_links:
            normalized = [
                [_linkify(cell, sheet_links.get((ri, ci))) for ci, cell in enumerate(row)]
                for ri, row in enumerate(normalized)
            ]

        header = normalized[0]
        parts.append("| " + " | ".join(header) + " |")
        parts.append("| " + " | ".join(["---"] * max_cols) + " |")
        for row in normalized[1:]:
            parts.append("| " + " | ".join(row) + " |")
        parts.append("")

    return "\n".join(parts)


_LINK_FIELDS = (
    "sheets(properties.title,data(startRow,startColumn,"
    "rowData.values(hyperlink,chipRuns.chip.richLinkProperties.uri,"
    "textFormatRuns.format.link.uri)))"
)


def _cell_links(cell: dict) -> list[str]:
    """Every URL a cell carries, in the three places Sheets stores them:
    a cell-level hyperlink, smart chips (the @-mention style Drive file
    chips Google inserts when you paste a Docs URL), and rich-text runs.
    """
    urls: list[str] = []
    if cell.get("hyperlink"):
        urls.append(cell["hyperlink"])
    for run in cell.get("chipRuns", []):
        uri = run.get("chip", {}).get("richLinkProperties", {}).get("uri")
        if uri:
            urls.append(uri)
    for run in cell.get("textFormatRuns", []):
        uri = run.get("format", {}).get("link", {}).get("uri")
        if uri:
            urls.append(uri)
    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def _sheet_hyperlinks(sheets_service, spreadsheet_id: str) -> dict[str, dict[tuple[int, int], list[str]]]:
    """{sheet title: {(row, col): [urls]}} for every linked cell.

    values().get() returns display text only, so a cell like "工程管理文件"
    that links to another Doc loses the link — and an index sheet that
    exists to point at other documents becomes a list of bare names. A
    second, fields-restricted grid fetch recovers the URLs. Best-effort:
    any failure just yields no links.
    """
    try:
        grid = api_call_with_retry(
            lambda: sheets_service.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, includeGridData=True, fields=_LINK_FIELDS)
            .execute()
        )
    except Exception:
        return {}
    out: dict[str, dict[tuple[int, int], list[str]]] = {}
    for sheet in grid.get("sheets", []):
        title = sheet.get("properties", {}).get("title", "")
        cells: dict[tuple[int, int], list[str]] = {}
        for block in sheet.get("data", []):
            r0 = block.get("startRow", 0)
            c0 = block.get("startColumn", 0)
            for ri, row in enumerate(block.get("rowData", [])):
                for ci, cell in enumerate(row.get("values", [])):
                    urls = _cell_links(cell)
                    if urls:
                        cells[(r0 + ri, c0 + ci)] = urls
        if cells:
            out[title] = cells
    return out


def _linkify(text: str, urls: list[str] | str | None) -> str:
    """Render a cell's text with its link(s) as Markdown.

    One link → `[text](url)`. Several (a cell listing multiple URLs as rich
    text) → keep the text and append any URL it doesn't already show.
    """
    if not urls or not text:
        return text
    if isinstance(urls, str):
        urls = [urls]
    missing = [u for u in urls if u not in text]
    if not missing:
        return text
    if len(urls) == 1:
        return f"[{text}]({urls[0]})"
    return text + " " + " ".join(f"<{u}>" for u in missing)


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MXFILE_MIME = "application/vnd.jgraph.mxfile"


def mxfile_to_xml(data: bytes) -> str:
    """Return a draw.io file as readable XML.

    draw.io usually stores each <diagram> as base64(deflate(urlencode(xml)));
    the agent can read mxGraph XML fine, but not that blob. Inflate every
    compressed diagram in place and leave already-plain ones untouched.
    """
    import base64
    import re
    import urllib.parse
    import zlib

    text = data.decode("utf-8", errors="replace")

    def _inflate(m: "re.Match[str]") -> str:
        body = m.group(2).strip()
        if not body or body.lstrip().startswith("<"):
            return m.group(0)
        try:
            raw = zlib.decompress(base64.b64decode(body), -15)
            xml = urllib.parse.unquote(raw.decode("utf-8"))
        except Exception:
            return m.group(0)
        return f"{m.group(1)}\n{xml}\n{m.group(3)}"

    return re.sub(r"(<diagram\b[^>]*>)(.*?)(</diagram>)", _inflate, text, flags=re.S)


def _mx_label(value: str | None) -> str:
    """draw.io labels are HTML: unescape, strip tags, collapse whitespace."""
    import html
    import re

    text = html.unescape(value or "")
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def mxfile_summary(xml: str) -> str:
    """A compact node/edge digest of a draw.io file, in Markdown.

    Prepended to the raw XML so the agent gets the whole architecture from the
    first kb_read window (~60 lines) instead of paging through hundreds of
    lines of mxCell markup. Nodes list their container (`⊂ parent`); edges are
    `source → target [label]`. Best-effort: unparsable XML yields a note.
    """
    from xml.etree import ElementTree as ET

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        return f"> 摘要無法產生（XML 解析失敗：{e}）；請直接讀下方 XML。\n"

    out = ["## 圖形摘要（自動由 XML 解析；完整內容見下方 XML）", ""]
    for diagram in root.iter("diagram"):
        cells = {c.get("id"): c for c in diagram.iter("mxCell")}

        def name(cid: str | None, depth: int = 0) -> str:
            cell = cells.get(cid or "")
            if cell is None:
                return "?"
            label = _mx_label(cell.get("value"))
            if label:
                return label
            parent = cell.get("parent")
            if depth < 4 and parent not in (None, "0", "1"):
                return f"[{name(parent, depth + 1)}]"
            return "(未命名)"

        nodes = [c for c in cells.values() if c.get("vertex") == "1" and _mx_label(c.get("value"))]
        edges = [c for c in cells.values() if c.get("edge") == "1"]
        # Edge labels are often separate child vertices of the edge.
        edge_labels: dict[str, list[str]] = {}
        for c in cells.values():
            parent = cells.get(c.get("parent") or "")
            if c.get("vertex") == "1" and parent is not None and parent.get("edge") == "1":
                label = _mx_label(c.get("value"))
                if label:
                    edge_labels.setdefault(parent.get("id"), []).append(label)
                    nodes = [n for n in nodes if n is not c]

        out.append(f"### 分頁：{diagram.get('name') or '(unnamed)'} — {len(nodes)} 個節點、{len(edges)} 條連線")
        out.append("")
        out.append("**節點**")
        for n in nodes:
            parent = cells.get(n.get("parent") or "")
            container = _mx_label(parent.get("value")) if parent is not None else ""
            out.append(f"- {_mx_label(n.get('value'))}" + (f" ⊂ {container}" if container else ""))
        out.append("")
        out.append("**連線（source → target）**")
        for e in edges:
            labels = [_mx_label(e.get("value"))] + edge_labels.get(e.get("id"), [])
            labels = [lb for lb in labels if lb]
            line = f"- {name(e.get('source'))} → {name(e.get('target'))}"
            if labels:
                line += f"  [{' / '.join(labels)}]"
            out.append(line)
        out.append("")
    return "\n".join(out)
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_bytes_to_text(data: bytes) -> str:
    """Extract plain text from .docx bytes (paragraphs + tables), stdlib only."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = ElementTree.fromstring(zf.read("word/document.xml"))

    lines: list[str] = []
    body = root.find(f"{_W_NS}body")
    for block in body if body is not None else []:
        if block.tag == f"{_W_NS}p":
            text = "".join(t.text or "" for t in block.iter(f"{_W_NS}t"))
            lines.append(text)
        elif block.tag == f"{_W_NS}tbl":
            for row in block.iter(f"{_W_NS}tr"):
                cells = [
                    "".join(t.text or "" for t in cell.iter(f"{_W_NS}t"))
                    for cell in row.iter(f"{_W_NS}tc")
                ]
                lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def convert_file_to_md(
    drive_service, sheets_service, file_meta: dict, docs_service=None
) -> str | None:
    """Convert a Google Drive file to Markdown content based on its MIME type."""
    file_id = file_meta["id"]
    mime = file_meta["mimeType"]

    if mime == "application/vnd.google-apps.document":
        try:
            # short retry budget — large docs fail export deterministically,
            # so fail over to the Docs API quickly instead of backing off
            text = export_google_doc(drive_service, file_id, max_retries=2)
        except (HttpError, TimeoutError, OSError):
            if docs_service is None:
                raise
            text = google_doc_to_text_via_docs_api(docs_service, file_id)
        return f"# {file_meta['name']}\n\n{text}"

    # Uploaded .docx is NOT export()-able (only Docs Editors files are) —
    # download the raw bytes and extract text locally.
    elif mime == _DOCX_MIME:
        data = api_call_with_retry(
            lambda: drive_service.files()
            .get_media(fileId=file_id, supportsAllDrives=True)
            .execute()
        )
        text = docx_bytes_to_text(data)
        return f"# {file_meta['name']}\n\n{text}"

    elif mime == "application/vnd.google-apps.spreadsheet":
        md = export_spreadsheet_as_md(sheets_service, file_id)
        return f"# {file_meta['name']}\n\n{md}"

    # draw.io diagrams: ship the (inflated) mxGraph XML — node labels and
    # source/target edges are all the agent needs to read an architecture.
    elif mime == _MXFILE_MIME:
        data = api_call_with_retry(
            lambda: drive_service.files()
            .get_media(fileId=file_id, supportsAllDrives=True)
            .execute()
        )
        xml = mxfile_to_xml(data)
        summary = mxfile_summary(xml)
        return f"# {file_meta['name']}\n\n{summary}\n```xml\n{xml}\n```"

    elif mime.startswith("text/"):
        content = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        text = content.decode("utf-8") if isinstance(content, bytes) else content
        return f"# {file_meta['name']}\n\n```\n{text}\n```"

    return None
