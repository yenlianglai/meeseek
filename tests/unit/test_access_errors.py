"""Missing access reads as missing access: a person without a role on the
Google project, or without a valid login, is told so instead of seeing a raw
error or being sent to the maintainer."""

from google.auth.exceptions import RefreshError
from google.genai.errors import ClientError, ServerError

from meeseek.config import cfg
from meeseek.runtime.llm import access_failure, failure_message, is_transient_failure


def _denied():
    return ClientError(403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                       "message": "Permission 'aiplatform.endpoints.predict' denied"}})


class TestAccessFailure:
    def test_a_403_from_the_model_is_denied(self):
        assert access_failure(_denied()) == "denied"

    def test_it_is_found_through_a_wrapping_exception(self):
        try:
            try:
                raise _denied()
            except ClientError as e:
                raise RuntimeError("plugin failed") from e
        except RuntimeError as wrapped:
            assert access_failure(wrapped) == "denied"

    def test_an_invalid_login_is_login(self):
        assert access_failure(RefreshError("Reauthentication is needed")) == "login"
        assert access_failure(ClientError(401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}})) == "login"

    def test_a_disabled_api_is_not_a_missing_role(self):
        err = ClientError(403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                          "message": "Vertex AI API has not been used (SERVICE_DISABLED)"}})
        assert access_failure(err) == ""

    def test_other_failures_are_neither(self):
        assert access_failure(ValueError("x")) == ""
        assert access_failure(ServerError(503, {"error": {"code": 503}})) == ""

    def test_missing_access_is_not_retried(self):
        assert not is_transient_failure(_denied())
        assert not is_transient_failure(RefreshError("Reauthentication is needed"))


class TestWhatThePersonReads:
    def test_denied_names_the_project_and_the_roles(self, monkeypatch):
        monkeypatch.setattr(cfg.google_cloud, "project", "acme-ml")
        monkeypatch.setattr(cfg.google_cloud, "quota_project", "acme-ml")
        text = failure_message(_denied())
        assert cfg.google_cloud.project in text
        assert "roles/aiplatform.user" in text and "roles/serviceusage.serviceUsageConsumer" in text
        assert "維護者" not in text

    def test_denied_on_the_examples_project_says_to_fill_it_in(self, monkeypatch):
        monkeypatch.setattr(cfg.google_cloud, "project", "your-gcp-project")
        text = failure_message(_denied())
        assert "google_cloud.project" in text and "Setup → 2" in text
        assert "roles/aiplatform.user" not in text      # no admin to ask: nothing is filled in yet

    def test_login_points_at_the_login_step(self):
        assert "Setup → 4" in failure_message(RefreshError("Reauthentication is needed"))

    def test_anything_else_is_the_plain_error(self):
        assert failure_message(ValueError("boom")) == "ValueError: boom"

    def test_the_console_says_what_missing_access_means(self, monkeypatch):
        """A turn that ends on a 403 from the model shows the person the roles
        to ask for, not a stack trace."""
        import json

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from meeseek import engine
        from meeseek.config import config
        from meeseek.surfaces import console

        async def denied(*a, **k):
            raise _denied()

        monkeypatch.setattr(config(), "mcp_servers", [])
        monkeypatch.setattr(cfg.google_cloud, "project", "acme-ml")
        monkeypatch.setattr(cfg.google_cloud, "quota_project", "acme-ml")
        monkeypatch.setattr(engine, "turn", denied)
        app = FastAPI()
        console.install(app)
        res = TestClient(app).post("/api/sessions/web-x/turns", json={"user": "webui", "text": "hi"})
        [frame] = [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: ")]
        assert frame["kind"] == "error"
        assert "roles/aiplatform.user" in frame["message"] and "Traceback" not in frame["message"]
