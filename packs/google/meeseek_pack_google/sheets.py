"""Google Sheets: read any sheet you can read; write only the allow-listed ones.

The allow list is the pack's `sheets_writable` setting (spreadsheet ids or
links). A write to any other spreadsheet is refused here, before the API is
called: the credential's `spreadsheets` scope can edit every sheet the person
can, so this list is the boundary. The writes are `writes=True` tools — a skill
uses them only when its `tools:` grants them, and each waits for a yes.
"""

from __future__ import annotations

import re

from meeseek_pack_google.auth import get_credentials
from meeseek_pack_google.gdrive import api_call_with_retry, build_service
from meeseek_pack_google.settings import settings

_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_MAX_CELLS = 20_000      # cells one read returns; past it, read a smaller range


def spreadsheet_id(ref: str) -> str:
    """A spreadsheet id from an id or any of its links."""
    ref = (ref or "").strip()
    m = _ID.search(ref)
    return m.group(1) if m else ref


def writable() -> set[str]:
    return {spreadsheet_id(s) for s in settings().sheets_writable if s}


def _check_writable(ref: str) -> str:
    sid = spreadsheet_id(ref)
    if sid not in writable():
        allowed = ", ".join(sorted(writable())) or "(none: mcp_servers.google.config.sheets_writable is empty)"
        raise PermissionError(f"Spreadsheet {sid} is not on the write allow list. Writable: {allowed}")
    return sid


def _svc():
    return build_service("sheets", "v4", get_credentials())


def _call(request):
    try:
        return api_call_with_retry(request.execute)
    except Exception as e:  # noqa: BLE001 — say what to do when it is the scope
        if "insufficient" in str(e).lower() and "scope" in str(e).lower():
            raise PermissionError("The Google login lacks the Sheets write scope: run ./scripts/setup.sh "
                                  "and sign in again") from e
        raise


def _a1(sheet: str) -> str:
    return "'" + sheet.replace("'", "''") + "'"


def sheets_read_values(spreadsheet: str, range: str = "") -> dict:  # noqa: A002 — the API's own word
    """The values of `range` (A1 notation, e.g. "'Tasks'!A1:F40" or "Tasks"):
    {range, values: [[...], ...]}; the sheet names when `range` is empty."""
    sid = spreadsheet_id(spreadsheet)
    svc = _svc()
    if not range:
        meta = _call(svc.spreadsheets().get(spreadsheetId=sid, fields="properties.title,sheets.properties.title"))
        return {"title": meta.get("properties", {}).get("title", ""),
                "sheets": [s["properties"]["title"] for s in meta.get("sheets", [])],
                "writable": sid in writable()}
    got = _call(svc.spreadsheets().values().get(spreadsheetId=sid, range=range, valueRenderOption="FORMATTED_VALUE"))
    values = got.get("values", [])
    cells = sum(len(r) for r in values)
    if cells > _MAX_CELLS:
        raise ValueError(f"{range} holds {cells} cells, over {_MAX_CELLS}: read a smaller range")
    return {"range": got.get("range", range), "values": values, "writable": sid in writable()}


def sheets_update_values(spreadsheet: str, data: list[dict], value_input_option: str = "USER_ENTERED") -> dict:
    """Write `data` — [{range, values}] in A1 notation — in one call. Only an
    allow-listed spreadsheet."""
    sid = _check_writable(spreadsheet)
    if not data or not all(isinstance(d, dict) and d.get("range") and isinstance(d.get("values"), list)
                           for d in data):
        raise ValueError("data is a list of {range, values}, values a list of rows")
    if value_input_option not in ("USER_ENTERED", "RAW"):
        raise ValueError("value_input_option is USER_ENTERED or RAW")
    body = {"valueInputOption": value_input_option,
            "data": [{"range": d["range"], "values": d["values"]} for d in data]}
    out = _call(_svc().spreadsheets().values().batchUpdate(spreadsheetId=sid, body=body))
    return {"spreadsheet": sid, "updated_cells": out.get("totalUpdatedCells", 0),
            "ranges": [r.get("updatedRange", "") for r in out.get("responses", [])]}


def sheets_append_or_replace(spreadsheet: str, sheet: str, key_column: str, rows: list[list],
                             header_rows: int = 1, value_input_option: str = "USER_ENTERED") -> dict:
    """Each of `rows` replaces the row whose `key_column` (a letter, e.g. "A")
    holds the same key, or is appended after the last row when none does. The
    key is the row's cell in that column. Only an allow-listed spreadsheet."""
    sid = _check_writable(spreadsheet)
    col = key_column.strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", col):
        raise ValueError("key_column is a column letter, e.g. A")
    if not rows or not all(isinstance(r, list) and r for r in rows):
        raise ValueError("rows is a list of non-empty rows")
    index = 0
    for ch in col:
        index = index * 26 + (ord(ch) - 64)
    index -= 1
    for r in rows:
        if len(r) <= index or str(r[index]).strip() == "":
            raise ValueError(f"every row needs its key in column {col}")
    svc = _svc()
    got = _call(svc.spreadsheets().values().get(spreadsheetId=sid, range=f"{_a1(sheet)}!{col}:{col}"))
    keys = [(r[0] if r else "") for r in got.get("values", [])]
    where = {str(k).strip(): n + 1 for n, k in enumerate(keys) if n >= header_rows and str(k).strip()}
    last = len(keys)
    data, replaced, appended = [], [], []
    for r in rows:
        key = str(r[index]).strip()
        if key in where:
            n = where[key]
            replaced.append(n)
        else:
            last += 1
            n = where[key] = last
            appended.append(n)
        data.append({"range": f"{_a1(sheet)}!A{n}", "values": [r]})
    out = _call(svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sid, body={"valueInputOption": value_input_option, "data": data}))
    return {"spreadsheet": sid, "sheet": sheet, "replaced_rows": replaced, "appended_rows": appended,
            "updated_cells": out.get("totalUpdatedCells", 0)}
