"""The GitLab pack's settings as meeseek would hand them over: the test repos."""

import json

PROJECTS = ["acme/platform/acme-app", "acme/acme-app/iac", "acme/infra/litellm"]
SETTINGS = {"base_url": "https://gitlab.com", "projects": PROJECTS, "skip_ssl_verify": False, "local_clone": ""}


def use(monkeypatch, **over):
    """These settings, with `over` changed, for the rest of the test."""
    monkeypatch.setenv("MEESEEK_PACK_CONFIG", json.dumps({**SETTINGS, **over}))
