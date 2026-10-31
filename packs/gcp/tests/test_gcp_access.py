"""Missing access reads as missing access: a GCP call the signed-in account
has no IAM role for says so, rather than looking like a broken tool."""

from meeseek_pack_gcp import gcp
from meeseek_pack_kit import http


class TestGcpTools:
    def test_a_403_says_the_account_lacks_the_role(self, monkeypatch):
        monkeypatch.setattr(gcp, "token", lambda force=False: "t")
        monkeypatch.setattr(http, "request", lambda *a, **k: {"error": "HTTP 403: Permission denied"})
        out = gcp.call("https://logging.googleapis.com/v2/entries:list", body={})
        assert out["error"].startswith("HTTP 403") and "IAM 權限" in out["error"]

    def test_other_errors_are_left_as_they_are(self, monkeypatch):
        monkeypatch.setattr(gcp, "token", lambda force=False: "t")
        monkeypatch.setattr(http, "request", lambda *a, **k: {"error": "HTTP 404: not found"})
        assert gcp.call("https://x")["error"] == "HTTP 404: not found"
