"""Unit tests for tools/web.py — URL fetch guardrails + text extraction."""

import pytest

from meeseek.tools import web


class TestExtractText:
    def test_drops_script_and_style_keeps_content(self):
        html = (
            "<html><head><style>body{color:red}</style></head>"
            "<body><p>This paragraph has enough real words to keep.</p>"
            "<script>evil()</script></body></html>"
        )
        text = web._extract_text(html)
        assert "This paragraph has enough real words to keep." in text
        assert "evil" not in text
        assert "color:red" not in text

    def test_drops_short_nav_lines(self):
        html = "<body><a>Home</a><a>About</a><p>A full sentence with several words here.</p></body>"
        text = web._extract_text(html)
        assert "A full sentence with several words here." in text
        assert "Home" not in text  # single-word nav dropped

    def test_malformed_markup_falls_back(self):
        # Unclosed tags shouldn't raise.
        text = web._extract_text("<p>content that is long enough to survive<")
        assert "content that is long enough to survive" in text


class TestFetchUrl:
    def test_rejects_non_http_scheme(self):
        assert "http" in web.fetch_url("ftp://example.com/file")

    def test_blocks_internal_host(self, monkeypatch):
        # ADK's fetcher raises ValueError for blocked hosts; surface it, don't crash.
        assert web.fetch_url("http://169.254.169.254/").startswith("拒絕抓取")

    def test_non_200_reports_failure(self, monkeypatch):
        class FakeResp:
            status_code = 404
            text = ""

        monkeypatch.setattr(web.safe_fetch, "get", lambda url: FakeResp())
        out = web.fetch_url("https://example.com/missing")
        assert "404" in out

    def test_success_returns_extracted_text(self, monkeypatch):
        class FakeResp:
            status_code = 200
            text = "<body><p>The quick brown fox jumps over the lazy dog.</p></body>"

        monkeypatch.setattr(web.safe_fetch, "get", lambda url: FakeResp())
        out = web.fetch_url("https://example.com/")
        assert "quick brown fox" in out

    def test_truncates_long_pages(self, monkeypatch):
        big = "<body>" + "<p>word word word word word word.</p>" * 5000 + "</body>"

        class FakeResp:
            status_code = 200
            text = big

        monkeypatch.setattr(web.safe_fetch, "get", lambda url: FakeResp())
        out = web.fetch_url("https://example.com/")
        assert "已截斷" in out
        assert len(out) < web._MAX_CHARS + 200


class TestGoogleLinks:
    """A Google Docs/Sheets link is a 401 over HTTP: it is read with the user's
    Google login instead (2026-10-14, 10-20: asked twice, failed twice)."""

    def test_the_file_id_comes_out_of_every_kind_of_link(self):
        fid = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCd"
        for url in (f"https://docs.google.com/spreadsheets/d/{fid}/edit?gid=123456#gid=123456",
                    f"https://docs.google.com/document/d/{fid}/edit?tab=t.0",
                    f"https://drive.google.com/file/d/{fid}/view",
                    f"https://drive.google.com/open?id={fid}"):
            assert web.google_file_id(url) == fid
        assert web.google_file_id(f"https://example.com/d/{fid}") is None

    def test_a_google_link_never_goes_over_plain_http(self, monkeypatch):
        monkeypatch.setattr(web, "read_google_file", lambda fid: f"# doc {fid}")
        monkeypatch.setattr(web.safe_fetch, "get", lambda url: pytest.fail("fetched over HTTP"))
        assert web.fetch_url("https://docs.google.com/document/d/1ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210_-ZyXw/edit") \
            == "# doc 1ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210_-ZyXw"

    def test_a_failure_reading_it_is_reported_not_raised(self, monkeypatch):
        def boom(fid):
            raise RuntimeError("no login")
        monkeypatch.setattr(web, "read_google_file", boom)
        assert "no login" in web.fetch_url("https://docs.google.com/document/d/1ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210_-ZyXw")
