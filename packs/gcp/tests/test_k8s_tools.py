"""Logs and pod status: the query is built by code from the environment, the
result is compact lines the user can read."""

from datetime import datetime, timezone

import pytest

from meeseek_pack_gcp import gcp, k8s

UAT = gcp.Env("uat-tokyo", "p-uat", "asia-northeast1", "c-uat", "acme-app")
PROD = gcp.Env("prod-tokyo", "p-prod", "asia-northeast1", "c-prod", "acme-app")
START = datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def envs(monkeypatch):
    monkeypatch.setattr(gcp, "envs", lambda: {e.name: e for e in (UAT, PROD)})


class TestResolveEnvs:
    def test_empty_means_every_env(self):
        assert gcp.resolve_envs("") == [UAT, PROD]

    def test_a_partial_name_matches(self):
        assert gcp.resolve_envs("uat") == [UAT]

    def test_an_unknown_env_lists_the_valid_ones(self):
        with pytest.raises(ValueError, match="uat-tokyo, prod-tokyo"):
            gcp.resolve_envs("staging")


class TestBuildFilter:
    def test_the_env_supplies_project_cluster_and_namespace(self):
        f = k8s.build_filter(UAT, service="litellm", severity="error", contains='say "hi"', start=START)
        assert 'resource.labels.project_id="p-uat"' in f
        assert 'resource.labels.cluster_name="c-uat"' in f
        assert 'resource.labels.namespace_name="acme-app"' in f
        assert 'resource.labels.pod_name:"litellm"' in f
        assert "severity>=ERROR" in f
        assert 'timestamp>="2026-10-09T01:00:00Z"' in f
        assert '"say \\"hi\\""' in f

    def test_a_namespace_overrides_the_env_default(self):
        assert 'namespace_name="litellm"' in k8s.build_filter(UAT, namespace="litellm", start=START)

    def test_another_resource_type_drops_the_cluster_clauses(self):
        f = k8s.build_filter(UAT, resource_type="cloudsql_database", start=START)
        assert "cluster_name" not in f and 'resource.type="cloudsql_database"' in f

    def test_a_bad_severity_is_refused(self):
        with pytest.raises(ValueError):
            k8s.build_filter(UAT, severity="LOUD", start=START)


class TestFormat:
    def test_json_payload_keeps_message_and_the_useful_keys(self):
        line = k8s.format_entry({
            "timestamp": "2026-10-09T01:02:03.456789Z", "severity": "ERROR",
            "resource": {"labels": {"pod_name": "acme-app-uat-1"}},
            "jsonPayload": {"message": "llm failed", "event": "llm_call", "session_id": "s1",
                            "extra": {"noise": 1}}})
        assert line.startswith("2026-10-09 01:02:03.456 ERROR    acme-app-uat-1 | llm failed")
        assert "event=llm_call" in line and "session_id=s1" in line and "noise" not in line

    def test_a_multiline_text_payload_becomes_one_line(self):
        line = k8s.format_entry({"timestamp": "", "textPayload": "Traceback\n  File x\nKeyError: 'a'"})
        assert "\n" not in line and "KeyError" in line

    def test_a_long_message_is_cut(self):
        assert k8s.format_entry({"textPayload": "x" * 2000}).endswith(" …")

    def test_a_pod_says_why_its_container_last_died(self):
        line = k8s.format_pod({
            "metadata": {"name": "acme-app-prod-1", "creationTimestamp": "2026-10-01T00:00:00Z"},
            "status": {"phase": "Running", "containerStatuses": [
                {"name": "backend", "ready": False, "restartCount": 4,
                 "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                 "lastState": {"terminated": {"reason": "OOMKilled"}}}]}})
        assert "0/1" in line and "4" in line
        assert "backend:CrashLoopBackOff" in line and "last: backend:OOMKilled" in line


class TestQueryLogs:
    def test_one_call_per_env_with_the_built_filter(self, monkeypatch):
        calls = []

        def fake_call(url, params=None, body=None, **_):
            calls.append(body)
            return {"entries": [{"timestamp": "2026-10-09T01:00:00Z", "severity": "WARNING",
                                 "textPayload": "slow"}], "nextPageToken": "t"}

        monkeypatch.setattr(gcp, "call", fake_call)
        out = k8s.query_logs("", service="backend", limit=500)
        assert [c["resourceNames"] for c in calls] == [["projects/p-uat"], ["projects/p-prod"]]
        assert all(c["pageSize"] == 50 for c in calls)
        assert out.count("```text") == 2 and "more exist" in out and "Cloud Console" in out

    def test_an_api_error_is_reported_with_the_filter(self, monkeypatch):
        monkeypatch.setattr(gcp, "call", lambda *a, **k: {"error": "HTTP 403: denied"})
        out = k8s.query_logs("uat")
        assert "Error: Cloud Logging HTTP 403" in out and "resource.labels.project_id" in out

    def test_a_bad_window_is_an_error_not_a_crash(self):
        assert k8s.query_logs("uat", since="yesterday").startswith("Error:")


def test_the_log_and_pod_tools_are_raw_evidence_reads():
    import meeseek_pack_gcp

    tools = {t.name: t for t in meeseek_pack_gcp.tools()}
    assert tools["query_logs"].shows_raw and tools["k8s_status"].shows_raw
    assert not any(t.writes for t in tools.values())
    assert tools["alerts_poll"].source


def test_a_401_is_retried_once_with_a_refreshed_token(monkeypatch):
    """Parallel tools share one credential; a token caught mid-refresh is retried."""
    forced, answers = [], [{"error": "HTTP 401: invalid"}, {"ok": 1}]
    monkeypatch.setattr(gcp, "token", lambda force=False: forced.append(force) or "t")
    monkeypatch.setattr(gcp, "_call", lambda *a: answers.pop(0))
    assert gcp.call("https://x") == {"ok": 1}
    assert forced == [False, True]
