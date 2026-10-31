"""The GCP pack's settings as meeseek would hand them over: two environments."""

import json

SETTINGS = {
    "default_namespace": "your-namespace", "project": "your-gcp-project",
    "environments": {
        "uat": {"context": "gke_your-uat-project_REGION_your-uat-cluster", "namespace": "your-namespace",
                "gcp_project": "your-uat-project"},
        "prod": {"context": "gke_your-prod-project_REGION_your-prod-cluster", "namespace": "your-namespace",
                 "gcp_project": "your-prod-project"},
    },
}


def use(monkeypatch, **over):
    monkeypatch.setenv("MEESEEK_PACK_CONFIG", json.dumps({**SETTINGS, **over}))
