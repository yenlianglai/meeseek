"""The route that makes rendered outputs visible in the console.

A diagram or a generated image is written under knowledge_base/outputs and
embedded in the answer as markdown. If that src is not a URL this server
answers, the console shows a broken-image chip for a file that exists.
"""

import pytest

from meeseek.config import OUTPUTS_URL_PREFIX, home, outputs_url

_PNG = b"\x89PNG\r\n\x1a\n" + b"stub"


class TestOutputsUrl:
    def test_file_under_outputs_becomes_a_served_url(self):
        assert outputs_url(home().outputs / "diagrams" / "d.png") == "/outputs/diagrams/d.png"

    def test_file_elsewhere_falls_back_to_the_home_relative_path(self):
        # Not a URL this server serves, so it must not pretend to be one.
        assert outputs_url(home().root / "README.md") == "README.md"

    def test_file_outside_the_repo_keeps_its_absolute_path(self, tmp_path):
        elsewhere = tmp_path / "elsewhere.png"
        assert outputs_url(elsewhere) == str(elsewhere.resolve())


class TestOutputsRoute:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from meeseek.server import create_app

        # No context manager: the lifespan starts the scheduler and warms every
        # MCP toolset, none of which this route needs.
        return TestClient(create_app(enable_scheduler=False))

    @pytest.fixture
    def png(self):
        path = home().outputs / "diagrams" / "test-outputs-route.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_PNG)
        yield path
        path.unlink(missing_ok=True)

    def test_serves_a_rendered_file(self, client, png):
        res = client.get(outputs_url(png))
        assert res.status_code == 200
        assert res.content == _PNG

    def test_the_url_the_tools_hand_the_model_is_the_one_that_is_served(self, client, png):
        assert outputs_url(png).startswith(OUTPUTS_URL_PREFIX + "/")
        assert client.get(f"{OUTPUTS_URL_PREFIX}/diagrams/{png.name}").status_code == 200

    def test_missing_file_is_a_404_not_the_console_html(self, client):
        res = client.get("/outputs/diagrams/nope.png")
        assert res.status_code == 404

    def test_cannot_escape_the_outputs_directory(self, client):
        # The console is the only reader; a path that climbs out of outputs would
        # turn this into a read-any-file endpoint.
        for attempt in ("/outputs/../../server.py", "/outputs/..%2f..%2fserver.py"):
            assert client.get(attempt).status_code in (400, 403, 404)

    def test_a_page_an_agent_wrote_runs_sandboxed_with_no_network(self, client):
        """Its script must not be the console: a page that could call the
        console's API could approve a held write. Opened in a tab or a frame,
        it gets an opaque origin and no fetch — from the server, not the tag."""
        page = home().outputs / "pages" / "test-outputs-route.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("<script>fetch('/inbox_api/items')</script>")
        try:
            res = client.get(outputs_url(page))
            csp = res.headers["content-security-policy"]
            assert res.status_code == 200 and csp.startswith("sandbox ")
            assert "allow-same-origin" not in csp and "connect-src 'none'" in csp
            assert client.get(f"{OUTPUTS_URL_PREFIX}/diagrams/x.svg").status_code == 404
        finally:
            page.unlink(missing_ok=True)

    def test_an_image_is_served_as_is(self, client, png):
        assert "content-security-policy" not in client.get(outputs_url(png)).headers
