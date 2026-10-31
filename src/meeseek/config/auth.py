"""Google credentials the core itself needs: Application Default Credentials
for Vertex AI and Google-managed MCP servers, and a service account to
impersonate when such a server should run with less than the person's own
IAM. Workspace (Drive, Gmail, Chat, Calendar) is the Google pack's, with its
own scopes and check (`python -m meeseek_pack_google.auth --check`).
"""

import google.auth
from google.auth.credentials import Credentials

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def get_impersonated_credentials(target_sa: str, scopes: list[str]) -> Credentials:
    """Short-lived credentials impersonating `target_sa`（permission 層降權用）.

    用途：GCP managed MCP 改以「唯讀 SA」身分呼叫，而非使用者本人的完整
    IAM——tool_filter 之外的第二道（憑證層）防線。需要呼叫者對該 SA 有
    roles/iam.serviceAccountTokenCreator。
    """
    from google.auth import impersonated_credentials

    source, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
    return impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=target_sa,
        target_scopes=scopes,
        lifetime=3600,
    )
