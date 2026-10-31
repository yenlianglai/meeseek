"""Unit tests for tools/gdrive.py docx extraction and shortcut aggregation."""

import io
import zipfile

from meeseek_pack_google.gdrive import _DOCX_MIME, convert_file_to_md, docx_bytes_to_text

_DOC_XML = """<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Hello </w:t></w:r><w:r><w:t>world</w:t></w:r></w:p>
    <w:p><w:r><w:t>第二段</w:t></w:r></w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>"""


def _fake_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", _DOC_XML)
    return buf.getvalue()


class TestDocxExtraction:
    def test_extracts_paragraphs_and_tables(self):
        text = docx_bytes_to_text(_fake_docx())
        assert "Hello world" in text
        assert "第二段" in text
        assert "| A | B |" in text


class TestConvertFileToMd:
    def test_docx_uses_get_media_not_export(self):
        calls = []

        class FakeFiles:
            def get_media(self, fileId, **kw):
                calls.append(("get_media", fileId))
                return self

            def export(self, **kw):  # Drive 403s an export of a .docx
                raise AssertionError("export() must not be called for .docx")

            def execute(self):
                return _fake_docx()

        class FakeDrive:
            def files(self):
                return FakeFiles()

        meta = {"id": "f1", "name": "spec.docx", "mimeType": _DOCX_MIME}
        md = convert_file_to_md(FakeDrive(), None, meta)
        assert calls == [("get_media", "f1")]
        assert "Hello world" in md

    def test_legacy_doc_is_unsupported(self):
        meta = {"id": "f2", "name": "old.doc", "mimeType": "application/msword"}
        assert convert_file_to_md(None, None, meta) is None

    def test_google_doc_falls_back_to_docs_api_on_export_failure(self):
        class FailingExportFiles:
            def export(self, **kw):
                return self

            def get_media(self, **kw):
                return self

            def execute(self):
                raise TimeoutError("The read operation timed out")

        class FakeDrive:
            def files(self):
                return FailingExportFiles()

        class FakeDocuments:
            def get(self, documentId, includeTabsContent):
                assert includeTabsContent is True
                return self

            def execute(self):
                return {"tabs": [{
                    "tabProperties": {"title": "未解決"},
                    "documentTab": {"body": {"content": [
                        {"paragraph": {"elements": [{"textRun": {"content": "Q1 內容"}}]}},
                    ]}},
                }]}

        class FakeDocs:
            def documents(self):
                return FakeDocuments()

        meta = {"id": "d1", "name": "問題討論",
                "mimeType": "application/vnd.google-apps.document"}
        md = convert_file_to_md(FakeDrive(), None, meta, docs_service=FakeDocs())
        assert "## 未解決" in md
        assert "Q1 內容" in md


class TestDocsTextExtraction:
    def test_paragraphs_and_table_cells_become_text(self):
        from meeseek_pack_google.gdrive import _extract_text_from_elements

        text = _extract_text_from_elements([
            {"paragraph": {"elements": [{"textRun": {"content": "hello\n"}}]}},
            {"table": {"tableRows": [{"tableCells": [
                {"content": [{"paragraph": {"elements": [{"textRun": {"content": "a"}}]}}]},
                {"content": [{"paragraph": {"elements": [{"textRun": {"content": "b"}}]}}]},
            ]}]}},
        ])
        assert text == "hello\na | b\n"


class TestSheetHyperlinks:
    def test_linkify_wraps_text_with_url(self):
        from meeseek_pack_google.gdrive import _linkify

        assert _linkify("工程管理文件", "https://docs.google.com/x") == "[工程管理文件](https://docs.google.com/x)"
        assert _linkify("plain", None) == "plain"
        # A cell whose text already is the URL stays as-is.
        assert _linkify("https://a.b", ["https://a.b"]) == "https://a.b"
        # Several links in one cell: keep the text, append the ones it hides.
        assert _linkify("問題釐清 / 方案", ["https://x/1", "https://x/2"]) == "問題釐清 / 方案 <https://x/1> <https://x/2>"

    def test_cell_links_reads_hyperlink_chips_and_rich_text(self):
        from meeseek_pack_google.gdrive import _cell_links

        cell = {
            "hyperlink": "https://h",
            "chipRuns": [{"chip": {"richLinkProperties": {"uri": "https://chip"}}}, {"startIndex": 3}],
            "textFormatRuns": [{"format": {"link": {"uri": "https://rich"}}}, {"format": {}}],
        }
        assert _cell_links(cell) == ["https://h", "https://chip", "https://rich"]
        assert _cell_links({}) == []

    def test_export_renders_hyperlinked_cells_as_markdown_links(self):
        from meeseek_pack_google.gdrive import export_spreadsheet_as_md

        class _Exec:
            def __init__(self, payload):
                self._p = payload

            def execute(self):
                return self._p

        class _Values:
            def get(self, spreadsheetId, range):
                return _Exec({"values": [["項目", "網址"], ["API 文件", "Acme App API"]]})

        class _Spreadsheets:
            def get(self, spreadsheetId, includeGridData=False, fields=None):
                if includeGridData:
                    return _Exec({"sheets": [{
                        "properties": {"title": "索引"},
                        "data": [{"rowData": [
                            {"values": [{}, {}]},
                            {"values": [{}, {"chipRuns": [{"chip": {"richLinkProperties": {"uri": "https://docs.google.com/document/d/abc/edit"}}}]}]},
                        ]}],
                    }]})
                return _Exec({"sheets": [{"properties": {"title": "索引"}}]})

            def values(self):
                return _Values()

        class _Service:
            def spreadsheets(self):
                return _Spreadsheets()

        md = export_spreadsheet_as_md(_Service(), "sid")
        assert "| API 文件 | [Acme App API](https://docs.google.com/document/d/abc/edit) |" in md
        assert "| 項目 | 網址 |" in md


class TestMxfile:
    def _compressed(self, xml: str) -> str:
        import base64
        import urllib.parse
        import zlib
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        raw = c.compress(urllib.parse.quote(xml).encode()) + c.flush()
        return base64.b64encode(raw).decode()

    def test_inflates_compressed_diagram(self):
        from meeseek_pack_google.gdrive import mxfile_to_xml

        inner = '<mxGraphModel><root><mxCell id="1" value="LLM Gateway"/></root></mxGraphModel>'
        blob = self._compressed(inner)
        data = f'<mxfile><diagram id="d1" name="架構">{blob}</diagram></mxfile>'.encode()
        out = mxfile_to_xml(data)
        assert 'value="LLM Gateway"' in out
        assert blob not in out

    def test_leaves_plain_diagram_alone(self):
        from meeseek_pack_google.gdrive import mxfile_to_xml

        data = b'<mxfile><diagram id="d1"><mxGraphModel><root/></mxGraphModel></diagram></mxfile>'
        assert mxfile_to_xml(data) == data.decode()

    def test_convert_wraps_mxfile_in_xml_fence(self):
        from meeseek_pack_google.gdrive import _MXFILE_MIME, convert_file_to_md

        class _Media:
            def execute(self):
                return b'<mxfile><diagram id="d"><mxGraphModel/></diagram></mxfile>'

        class _Files:
            def get_media(self, fileId, supportsAllDrives=True):
                return _Media()

        class _Drive:
            def files(self):
                return _Files()

        md = convert_file_to_md(_Drive(), None, {"id": "x", "name": "架構.drawio", "mimeType": _MXFILE_MIME})
        assert md.startswith("# 架構.drawio\n\n")
        assert "```xml\n<mxfile>" in md
        assert md.rstrip().endswith("```")


class TestMxfileSummary:
    XML = (
        '<mxfile><diagram id="d" name="第 1 页"><mxGraphModel><root>'
        '<mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="box" value="&lt;b&gt;API Server&lt;/b&gt;" vertex="1" parent="1"/>'
        '<mxCell id="a" value="Safety&lt;br&gt;DLP" vertex="1" parent="box"/>'
        '<mxCell id="b" value="LiteLLM Proxy" vertex="1" parent="1"/>'
        '<mxCell id="e1" edge="1" source="a" target="b" parent="1"/>'
        '<mxCell id="l1" value="LLM 呼叫" vertex="1" parent="e1"/>'
        '<mxCell id="e2" edge="1" value="HTTPS" source="b" target="zzz" parent="1"/>'
        '</root></mxGraphModel></diagram></mxfile>'
    )

    def test_lists_nodes_with_container_and_edges_with_labels(self):
        from meeseek_pack_google.gdrive import mxfile_summary

        s = mxfile_summary(self.XML)
        assert "第 1 页" in s and "3 個節點、2 條連線" in s
        assert "- API Server" in s
        assert "- Safety DLP ⊂ API Server" in s
        assert "- Safety DLP → LiteLLM Proxy  [LLM 呼叫]" in s
        assert "- LiteLLM Proxy → ?  [HTTPS]" in s
        # edge-label vertices are not listed as nodes
        assert "- LLM 呼叫" not in s

    def test_summary_precedes_xml_in_converted_doc(self):
        from meeseek_pack_google.gdrive import _MXFILE_MIME, convert_file_to_md

        xml = self.XML.encode()

        class _Media:
            def execute(self):
                return xml

        class _Files:
            def get_media(self, fileId, supportsAllDrives=True):
                return _Media()

        class _Drive:
            def files(self):
                return _Files()

        md = convert_file_to_md(_Drive(), None, {"id": "x", "name": "架構.drawio", "mimeType": _MXFILE_MIME})
        assert md.index("## 圖形摘要") < md.index("```xml")

    def test_unparsable_xml_degrades_to_note(self):
        from meeseek_pack_google.gdrive import mxfile_summary

        assert "摘要無法產生" in mxfile_summary("<mxfile><diagram>")
