"""Sheets: read any, write only the allow-listed ones; upsert by a key column."""

import pytest
from google_test_settings import use

from meeseek_pack_google import sheets

SID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCd"
LINK = f"https://docs.google.com/spreadsheets/d/{SID}/edit?gid=123#gid=123"


class _Req:
    def __init__(self, out):
        self.out = out

    def execute(self):
        return self.out


class _Values:
    def __init__(self, column):
        self.column, self.writes = column, []

    def get(self, spreadsheetId, range, **kw):
        return _Req({"range": range, "values": [[k] for k in self.column]})

    def batchUpdate(self, spreadsheetId, body):
        self.writes.append((spreadsheetId, body))
        return _Req({"totalUpdatedCells": sum(len(d["values"][0]) for d in body["data"]),
                     "responses": [{"updatedRange": d["range"]} for d in body["data"]]})


def _service(monkeypatch, column=()):
    values = _Values(list(column))
    svc = type("S", (), {"spreadsheets": lambda _: type("P", (), {"values": lambda _: values})()})()
    monkeypatch.setattr(sheets, "_svc", lambda: svc)
    monkeypatch.setattr(sheets, "api_call_with_retry", lambda fn: fn())
    return values


def test_an_id_comes_out_of_a_link():
    assert sheets.spreadsheet_id(LINK) == SID
    assert sheets.spreadsheet_id(SID) == SID


def test_a_sheet_off_the_allow_list_is_refused_before_any_call(monkeypatch):
    values = _service(monkeypatch)
    with pytest.raises(PermissionError, match="allow list"):
        sheets.sheets_update_values(SID, [{"range": "A1", "values": [["x"]]}])
    with pytest.raises(PermissionError, match="allow list"):
        sheets.sheets_append_or_replace(SID, "Tasks", "A", [["k", "x"]])
    assert values.writes == []


def test_an_allow_listed_sheet_is_written_by_id_or_link(monkeypatch):
    use(monkeypatch, sheets_writable=[LINK])
    values = _service(monkeypatch)
    out = sheets.sheets_update_values(SID, [{"range": "'Tasks'!C7", "values": [["Done"]]}])
    assert out["updated_cells"] == 1 and values.writes[0][0] == SID
    assert values.writes[0][1]["valueInputOption"] == "USER_ENTERED"


def test_a_known_key_replaces_its_row_and_a_new_one_is_appended(monkeypatch):
    use(monkeypatch, sheets_writable=[SID])
    values = _service(monkeypatch, column=["Key", "T-1", "T-2", "", "T-3"])
    out = sheets.sheets_append_or_replace(SID, "Tasks", "A", [["T-2", "doing"], ["T-9", "new"], ["T-9", "newer"]])
    assert out["replaced_rows"] == [3, 6] and out["appended_rows"] == [6]   # T-9 once appended, then itself
    ranges = [d["range"] for d in values.writes[0][1]["data"]]
    assert ranges == ["'Tasks'!A3", "'Tasks'!A6", "'Tasks'!A6"]


def test_a_header_row_is_never_a_key(monkeypatch):
    use(monkeypatch, sheets_writable=[SID])
    _service(monkeypatch, column=["T-1", "T-2"])
    out = sheets.sheets_append_or_replace(SID, "Tasks", "A", [["T-1", "x"]], header_rows=1)
    assert out["appended_rows"] == [3] and out["replaced_rows"] == []


def test_every_row_needs_its_key(monkeypatch):
    use(monkeypatch, sheets_writable=[SID])
    _service(monkeypatch)
    with pytest.raises(ValueError, match="key in column B"):
        sheets.sheets_append_or_replace(SID, "Tasks", "B", [["T-1"]])
