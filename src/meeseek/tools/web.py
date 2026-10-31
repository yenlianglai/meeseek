"""Web page fetch — read the text of a public URL.

Model-agnostic (works with any litellm model, unlike Gemini's built-in
`url_context` which is Gemini-only). Fetched through tools/safe_fetch.py, which
lets no URL reach a non-public address and follows no redirect, then reduced
to readable text with the stdlib only.

A Google Docs/Sheets/Slides/Drive link is not a public page: fetched over
HTTP it is a 401. The Google pack reads it through the Drive API with the
user's own (read-only) Google login instead, as the Drive sync reads files —
every tab of a spreadsheet, a Doc as text. The whole of a long file is kept
under outputs/drive/ for kb_read.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

from meeseek.tools import safe_fetch

_MAX_CHARS = 20000
# Tags whose *content* is never readable page text.
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping script/style and collapsing whitespace."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        return "\n".join(self._parts)


def _extract_text(raw_html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw_html)
    except Exception:
        # Malformed markup — fall back to a crude tag strip.
        stripped = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw_html)
        stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
        return html.unescape(stripped)
    text = parser.text()
    # Keep lines with a bit of substance (drops single-word nav/menu noise).
    lines = [ln.strip() for ln in text.splitlines() if len(ln.split()) > 3]
    return "\n".join(lines)


_GOOGLE_HOSTS = {"docs.google.com", "drive.google.com"}


def google_file_id(url: str) -> str | None:
    """The Drive file id in a Google Docs/Sheets/Slides/Drive link; None for any other URL."""
    if (urlparse(url).hostname or "") not in _GOOGLE_HOSTS:
        return None
    m = re.search(r"/d/([\w-]{20,})", url) or re.search(r"[?&]id=([\w-]{20,})", url)
    return m.group(1) if m else None


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def read_google_file(file_id: str) -> str:
    """A Drive file as text, read with the user's Google login (the Google pack's drive_read)."""
    from meeseek.config.paths import home
    from meeseek.runtime import packs

    try:
        got = packs.call_json("drive_read", {"file_id": file_id})
    except LookupError:
        return "This is a Google Docs/Sheets/Drive link, and no Google pack is running to read it with your login."
    if "error" in got:
        return got["error"]
    md = got["markdown"]
    if len(md) <= _MAX_CHARS:
        return md
    out = home().outputs / "drive" / f"{_safe_name(got['name'])}-{file_id[:8]}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    rel = out.relative_to(home().outputs.parent)
    return (md[:_MAX_CHARS] + f"\n\n…（只顯示前 {_MAX_CHARS} 字元，全文 {len(md)} 字元已存成 `{rel}`："
            f"用 kb_read('{rel}', start_line=…) 讀其餘部分，或 kb_grep 在裡面找）")


def fetch_url(url: str) -> str:
    """Fetch a public web page and return its readable text (truncated).

    Args:
        url: An http(s) URL to read.

    Returns:
        The page's text content, or an error message if it can't be fetched.
    """
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return f"拒絕抓取：只支援 http(s) URL（收到：{url!r}）。"
    if file_id := google_file_id(url):
        try:
            return read_google_file(file_id)
        except Exception as e:  # noqa: BLE001 — the login itself, the network: say which
            return f"讀取 Google 檔案失敗：{type(e).__name__}: {str(e)[:300]}"
    try:
        response = safe_fetch.get(url)
    except ValueError as e:  # blocked host (SSRF guard)
        return f"拒絕抓取：{e}"
    except Exception as e:
        return f"抓取失敗：{url}（{type(e).__name__}: {e}）"

    if getattr(response, "status_code", None) != 200:
        code = getattr(response, "status_code", "?")
        return f"抓取失敗：{url}（HTTP {code}）"

    text = _extract_text(response.text)
    if not text.strip():
        return f"（{url} 沒有可讀取的文字內容）"
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + f"\n\n…（已截斷，原長度 {len(text)} 字元）"
    return text
