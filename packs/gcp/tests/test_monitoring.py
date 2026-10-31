"""Cloud Monitoring alert tools, offline: project resolution (a wrong project
answers "no alerts" for an environment on fire) and the fields the model
navigates by."""

import pytest

from meeseek_pack_gcp import monitoring
from meeseek_pack_gcp.settings import settings


class TestProjectResolution:
    def test_default_is_every_env_project(self):
        own = {e.gcp_project for e in settings().environments.values()}
        assert set(monitoring._resolve_projects("")) == own

    def test_env_alias_and_partial_alias_resolve(self):
        for alias, env in settings().environments.items():
            assert monitoring._resolve_projects(alias) == [env.gcp_project]
        prod = [n for n in settings().environments if n.startswith("prod")]
        if prod:
            assert monitoring._resolve_projects("prod") == [
                settings().environments[prod[0]].gcp_project
            ]

    def test_bare_project_id_resolves(self):
        target = settings().environments[
            next(iter(settings().environments))].gcp_project
        assert monitoring._resolve_projects(target) == [target]

    def test_unknown_project_rejected_with_options(self):
        with pytest.raises(ValueError, match="可用環境"):
            monitoring._resolve_projects("staging")


class TestFormatting:
    def test_subject_line_uses_metric_labels(self):
        """Log-based-metric alerts carry only project_id in resource.labels;
        without metric.labels every incident of one policy looks identical."""
        line = monitoring._subject_line({
            "resource": {"type": "k8s_container",
                         "labels": {"project_id": "p", "namespace_name": "acme-app"}},
            "metric": {"type": "logging.googleapis.com/user/acme-app-text-event-count",
                       "labels": {"event": "reranker.breaker_open"}},
        })
        assert "acme-app" in line
        assert "event=reranker.breaker_open" in line
        assert "p" not in line.split(" / ")  # project_id is already in the header

    def test_age_of_an_open_incident_is_measured_to_now(self):
        assert monitoring._age("2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z") == "10m"
        assert monitoring._age("2026-01-01T00:00:00Z", "2026-01-01T02:05:00Z") == "2h05m"
        assert monitoring._age("2026-01-01T00:00:00Z", "") != "?"  # open → now, not an error
        assert monitoring._age("garbage", "") == "?"


class TestErrorsAreReportedNotRaised:
    def test_api_error_is_returned_per_project(self, monkeypatch):
        """A 403 on one project must not lose the others' results."""
        calls = []

        def fake_get(path, params=None):
            calls.append(path)
            if len(calls) == 1:
                return {"error": "HTTP 403: permission denied"}
            return {"alerts": [], "totalSize": 0}

        monkeypatch.setattr(monitoring, "_get", fake_get)
        out = monitoring.list_alerts()
        assert "403" in out
        assert len(calls) == len(monitoring._resolve_projects(""))

    def test_bare_policy_id_without_project_asks_instead_of_guessing(self):
        if len(monitoring._resolve_projects("")) < 2:
            pytest.skip("single configured environment — the id is unambiguous")
        out = monitoring.get_alert_policy("123456789")
        assert "裸 id" in out
