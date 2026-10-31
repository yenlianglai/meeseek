"""Google Cloud REST calls with ADC, and the environments project.yaml names.

One token serves Cloud Logging, Cloud Monitoring, the GKE API and each
cluster's Kubernetes API (GKE accepts Google OAuth tokens), so the tools that
read them need no kubectl and no gcloud login — only application-default
credentials.
"""

from __future__ import annotations

import base64
import ssl
import threading
import urllib.parse
from dataclasses import dataclass

import google.auth
import google.auth.transport.requests

from meeseek_pack_kit import http

from meeseek_pack_gcp.settings import settings

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# Every call runs as the person signed in on this machine (their ADC), so a
# 403 is their account missing an IAM role, which a retry does not change.
_NO_ACCESS = "目前登入的 Google 帳號在這個 GCP 專案沒有這項 IAM 權限（需要專案管理員加角色，重試結果相同）"

_creds = None
# Tools run on pool threads; one refresh at a time, or a reader can get the
# token while another thread is replacing it.
_creds_lock = threading.Lock()


def token(force: bool = False) -> str:
    """Access token from ADC. Only cloud-platform is needed (no Workspace scope)."""
    global _creds
    with _creds_lock:
        if _creds is None:
            _creds, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        if force or not _creds.valid:
            _creds.refresh(google.auth.transport.requests.Request())
        return _creds.token


def call(url: str, params: dict | None = None, body: dict | None = None,
         context: ssl.SSLContext | None = None, timeout: int = 20) -> dict:
    """GET (or POST, with `body`) a JSON API. Returns {"error": ...} instead of raising.
    A 401 is retried once with a freshly refreshed token."""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    out = _call(url, body, context, timeout, token())
    if out.get("error", "").startswith("HTTP 401"):
        out = _call(url, body, context, timeout, token(force=True))
    if out.get("error", "").startswith("HTTP 403"):
        out["error"] += f" — {_NO_ACCESS}"
    return out


def _call(url: str, body: dict | None, context: ssl.SSLContext | None, timeout: int,
          bearer: str) -> dict:
    return http.request(url, method="POST" if body is not None else "GET", body=body,
                        headers={"Authorization": f"Bearer {bearer}"}, context=context,
                        timeout=timeout)


@dataclass(frozen=True)
class Env:
    name: str
    project: str
    location: str
    cluster: str
    namespace: str


def _parse(name: str, env) -> Env:
    # A GKE kubectl context is gke_<project>_<location>_<cluster>.
    parts = (env.context or "").split("_", 3)
    project, location, cluster = (parts[1:4] if len(parts) == 4 and parts[0] == "gke"
                                  else (env.gcp_project, "", ""))
    return Env(name=name, project=env.gcp_project or project, location=location,
               cluster=cluster, namespace=env.namespace or settings().default_namespace)


def envs() -> dict[str, Env]:
    return {n: _parse(n, e) for n, e in settings().environments.items()}


def resolve_envs(env: str) -> list[Env]:
    """Env name or part of one ('prod', 'uat') → the environments to query.

    Empty means every environment. Raises with the valid names otherwise, so
    the tool-error loop tells the model what it may ask for.
    """
    e = (env or "").strip()
    known = envs()
    if not e:
        return list(known.values())
    if e in known:
        return [known[e]]
    hits = [x for n, x in known.items() if e in n]
    if hits:
        return hits
    raise ValueError(f"'{env}' 不是已設定的環境。可用環境：{', '.join(known)}")


_clusters: dict[str, tuple[str, ssl.SSLContext]] = {}


def kube(env: Env, path: str, params: dict | None = None) -> dict:
    """GET a Kubernetes API path on the env's cluster, e.g. /api/v1/namespaces/x/pods."""
    if env.name not in _clusters:
        if not env.cluster:
            return {"error": f"{env.name} 沒有 GKE context（gke_<project>_<location>_<cluster>）"}
        c = call(f"https://container.googleapis.com/v1/projects/{env.project}"
                 f"/locations/{env.location}/clusters/{env.cluster}")
        if "error" in c:
            return c
        ca = base64.b64decode(c["masterAuth"]["clusterCaCertificate"]).decode()
        _clusters[env.name] = (c["endpoint"], ssl.create_default_context(cadata=ca))
    endpoint, context = _clusters[env.name]
    return call(f"https://{endpoint}{path}", params=params, context=context)
