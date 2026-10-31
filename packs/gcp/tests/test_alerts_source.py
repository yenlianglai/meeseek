"""The alerts source: open incidents as trigger items, each keyed by the incident."""

import meeseek_pack_gcp
from meeseek_pack_gcp import monitoring


def test_open_incidents_become_alert_items(monkeypatch):
    asked = []

    def get(path, params=None):
        asked.append(path)
        return {"alerts": [{"name": f"{path}/a1", "openTime": "2026-10-10T01:00:00Z",
                            "policy": {"displayName": "5xx", "severity": "ERROR", "name": "projects/p/alertPolicies/9"},
                            "resource": {"labels": {"project_id": "p"}}}]}

    monkeypatch.setattr(meeseek_pack_gcp, "_get", get)
    out = meeseek_pack_gcp.alerts_poll(since=0, state={"x": 1})
    assert sorted(asked) == ["projects/your-prod-project/alerts", "projects/your-uat-project/alerts"]
    [first, _] = out["items"]
    assert first["kind"] == "alert" and first["key"].startswith("alert:projects/")
    assert first["title"] == "[ERROR] 5xx" and first["meta"]["policy"] == "projects/p/alertPolicies/9"
    assert out["state"] == {"x": 1}


def test_a_source_that_reaches_no_project_fails_loudly(monkeypatch):
    import pytest

    monkeypatch.setattr(meeseek_pack_gcp, "_get", lambda path, params=None: {"error": "HTTP 403: denied"})
    with pytest.raises(RuntimeError, match="403"):
        meeseek_pack_gcp.alerts_poll()


def test_placeholders_list_the_environments():
    p = meeseek_pack_gcp.placeholders()
    assert p["k8s_env_names"] == "uat, prod" and "**prod** — project `your-prod-project`" in p["k8s_environments"]
    assert monitoring._subject_line({"resource": {"labels": {}}}) is not None
