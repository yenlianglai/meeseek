"""One JSON-over-HTTPS call for a pack's REST adapters.

A failure is a value, not an exception: `{"error": "HTTP 403: <the API's own
message>", "url": ...}` or `{"error": "Connection failed: ...", ...}`, so every
tool reports it the same way and the model reads what the server said.
"""

from __future__ import annotations

import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _api_message(raw: bytes) -> str:
    """The human part of an error body, in the shapes these APIs use."""
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return raw.decode("utf-8", errors="replace").strip()[:300]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):               # Google: {"error": {"message": ...}}
            return str(err.get("message", ""))
        msgs = body.get("errorMessages") or []  # Jira
        fields = body.get("errors") or {}
        if msgs or fields:
            return "; ".join([*map(str, msgs), *(f"{k}: {v}" for k, v in fields.items())])
        for key in ("message", "error_description", "error"):  # GitLab and others
            if body.get(key):
                return str(body[key])
    return json.dumps(body, ensure_ascii=False)[:300]


def request(url: str, *, method: str = "GET", headers: dict | None = None,
            body: dict | None = None, context: ssl.SSLContext | None = None,
            timeout: int = 20, text: bool = False) -> dict | list | str:
    """Parsed JSON (or the text body with `text`), or an {"error": ...} dict."""
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = Request(url, data=data, method=method, headers=hdrs)
    try:
        with urlopen(req, timeout=timeout, context=context) as resp:  # noqa: S310 — https hosts from config
            raw = resp.read()
    except HTTPError as e:
        detail = ""
        try:
            detail = _api_message(e.read())
        except Exception:  # noqa: BLE001 — the body is best-effort
            pass
        return {"error": f"HTTP {e.code}: {detail or e.reason}", "url": url}
    except (URLError, OSError) as e:
        return {"error": f"Connection failed: {getattr(e, 'reason', e)}", "url": url}
    if text:
        return raw.decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}
