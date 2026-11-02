"""Google Workspace authentication — single mode: ADC.

The ADC is created once per developer with the project's internal OAuth
client (org consent wall does not apply to internal apps). Don't type the
command by hand — `./scripts/setup.sh` builds it from WORKSPACE_SCOPES so
the list can't drift:

    gcloud auth application-default login \\
      --client-id-file=client_secret.json \\
      --scopes="$(python -m meeseek_pack_google.auth --login-scopes)"

client_secret.json: GCP Console → your project → Credentials → OAuth client
(Desktop app) → download; safe to delete after login. One ADC covers
everything: Vertex AI, GCP managed MCP, Drive/Docs/Sheets, Gmail, Chat.

**A plain `gcloud auth application-default login` silently breaks every
Workspace tool.** An `authorized_user` ADC has its scopes fixed at consent
time, and `google.auth.default(scopes=...)` does NOT apply them —
`requires_scopes` is False for user credentials, so the argument is
discarded without a word. `check_workspace_scopes()` exists to turn that
silence into an actionable error; `scripts/{setup,serve,doctor}.sh` all
call it via `python -m meeseek_pack_google.auth --check`.
"""

import json
import os
import time
import urllib.error
import urllib.request

import google.auth
from google.auth.credentials import Credentials

# The GCP half of the ADC. Present on any `gcloud auth application-default
# login`, so it is never the missing one.
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# The Workspace half — the part a default login omits. Single source of truth:
# setup.sh reads this list instead of hardcoding its own copy.
WORKSPACE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

# The one Workspace write: Sheets, for the spreadsheets the pack's
# `sheets_writable` allow list names (sheets.py refuses every other one).
SHEETS_WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# What the gcloud login must grant. Kept apart from WORKSPACE_SCOPES so "the
# Workspace reads are read-only" stays assertable (tests/test_google_scopes.py):
# cloud-platform is not .readonly, and the Sheets write scope is the one
# write, named on its own.
LOGIN_SCOPES = [CLOUD_PLATFORM_SCOPE, *WORKSPACE_SCOPES, SHEETS_WRITE_SCOPE]

_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

# Re-checked on this interval so a refresh token that dies mid-run (an OAuth
# client in "Testing" status expires them after 7 days) surfaces within one
# interval.
_CHECK_TTL_SEC = 300

_LOGIN_HINT = "run ./scripts/setup.sh (it rebuilds the ADC with the Workspace scopes)"

_cached: tuple[float, dict] | None = None


def _fetch_granted_scopes(token: str) -> set[str]:
    """Scopes the token actually carries, straight from Google.

    Deliberately not `credentials.has_scopes()`: an authorized_user loaded
    from the ADC file has `scopes = None` and `granted_scopes = None` even
    after a refresh, so has_scopes() returns False for scopes the token
    demonstrably holds. Using it as a gate would false-negative every
    Workspace tool. tokeninfo is the only authoritative source.
    """
    url = f"{_TOKENINFO_URL}?access_token={token}"
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 — fixed https host
        return set((json.load(resp).get("scope") or "").split())


def check_workspace_scopes(force: bool = False) -> dict:
    """Whether the ADC can reach the Workspace APIs. Result is TTL-cached.

    Returns `{"ok": True}`, or `{"ok": False, "missing": [...], "reason": str,
    "fix": str}`. Never raises.
    """
    global _cached

    if not force and _cached and time.monotonic() - _cached[0] < _CHECK_TTL_SEC:
        return _cached[1]

    result = _run_check()
    _cached = (time.monotonic(), result)
    return result


def _run_check() -> dict:
    def bad(reason: str, missing: list[str] | None = None) -> dict:
        return {
            "ok": False,
            "missing": missing if missing is not None else list(WORKSPACE_SCOPES),
            "reason": reason,
            "fix": _LOGIN_HINT,
        }

    try:
        credentials, _ = google.auth.default(scopes=WORKSPACE_SCOPES)
    except google.auth.exceptions.DefaultCredentialsError as e:
        return bad(f"no ADC found ({e.__class__.__name__})")

    try:
        # token is None until the first refresh, and tokeninfo needs a real one.
        if not credentials.valid:
            from google.auth.transport.requests import Request

            credentials.refresh(Request())
    except Exception as e:  # invalid_grant = refresh token revoked or expired
        return bad(f"ADC refresh failed: {e}")

    if credentials.requires_scopes:
        # Service-account credentials DO honour the scopes argument, so
        # google.auth.default() already applied WORKSPACE_SCOPES. Nothing to check.
        return {"ok": True}

    try:
        granted = _fetch_granted_scopes(credentials.token)
    except (urllib.error.URLError, OSError, ValueError) as e:
        # Offline or tokeninfo unreachable: unknown, not broken. Saying "ok"
        # keeps a network blip from raising a false alarm in the UI.
        return {"ok": True, "unverified": f"tokeninfo unreachable: {e}"}

    missing = [s for s in WORKSPACE_SCOPES if s not in granted]
    if missing:
        names = ", ".join(s.rsplit("/", 1)[-1] for s in missing)
        return bad(f"the login lacks {names}; what needs it will fail", missing)
    return {"ok": True}


def get_credentials() -> Credentials:
    """Google API credentials from ADC (refresh handled by the transport).

    Used only by the Workspace adapters (Chat, Gmail, Drive, Docs, Sheets);
    the GCP side builds its own (the GCP pack). 403 insufficient
    scopes → the ADC was created without the Workspace scopes; see the module
    docstring, and `check_workspace_scopes()` for the diagnosis.
    """
    credentials, _ = google.auth.default(scopes=WORKSPACE_SCOPES)

    # Pin the quota project to project.yaml's `google_cloud.quota_project`
    # (meeseek exports it as GCP_PROJECT_ID); otherwise calls bill to
    # whatever quota_project_id the ADC file holds, which can be stale and
    # surface as SERVICE_DISABLED.
    quota_project = os.environ.get("GCP_PROJECT_ID", "")
    if quota_project and hasattr(credentials, "with_quota_project"):
        credentials = credentials.with_quota_project(quota_project)
    return credentials


def _main() -> int:
    """`python -m meeseek_pack_google.auth --check|--login-scopes`.

    The shell scripts' entry point, so the scope list lives in exactly one
    place. --check exits 1 when scopes are missing; --login-scopes prints the
    comma-joined list for gcloud's --scopes flag (both halves).
    """
    import sys

    if "--login-scopes" in sys.argv:
        print(",".join(LOGIN_SCOPES))
        return 0

    result = check_workspace_scopes(force=True)
    if result.get("ok"):
        if result.get("unverified"):
            print(f"! Workspace scopes unverified — {result['unverified']}")
        else:
            print("✓ Google ADC has the Workspace scopes")
        return 0

    print(f"✗ Google ADC: {result['reason']}")
    for scope in result["missing"]:
        print(f"    missing: {scope}")
    print(f"  Workspace tools (Chat, Gmail, Drive, Docs) will fail — {result['fix']}")
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
