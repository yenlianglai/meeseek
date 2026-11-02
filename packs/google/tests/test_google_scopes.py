"""Workspace reads are read-only at the credential layer: the scopes the pack
reads with are .readonly, and the login adds only cloud-platform (which Vertex
and Google-managed MCP need) and the one write, Sheets — which the pack uses
only on the allow-listed spreadsheets."""

from meeseek_pack_google.auth import CLOUD_PLATFORM_SCOPE, LOGIN_SCOPES, SHEETS_WRITE_SCOPE, WORKSPACE_SCOPES

import meeseek_pack_google


def test_workspace_scopes_are_readonly():
    non_ro = [s for s in WORKSPACE_SCOPES if not s.endswith(".readonly")]
    assert non_ro == [], f"Workspace scopes 出現非唯讀: {non_ro}"


def test_login_scopes_add_only_cloud_platform():
    """Any new write scope trying to ride along is stopped here, not granted with the Workspace set."""
    extra = [s for s in LOGIN_SCOPES if s not in WORKSPACE_SCOPES]
    assert extra == [CLOUD_PLATFORM_SCOPE, SHEETS_WRITE_SCOPE], f"登入 scope 多了未預期的項目: {extra}"


def test_the_pack_writes_only_sheets_and_hides_what_is_meeseeks_own():
    tools = {t.name: t for t in meeseek_pack_google.tools()}
    assert {n for n, t in tools.items() if t.writes} == {"sheets_update_values", "sheets_append_or_replace"}
    assert {n for n, t in tools.items() if not (t.internal or t.source)} == {
        "search_gmail", "get_gmail_thread", "list_chat_spaces", "get_chat_messages",
        "sheets_read_values", "sheets_update_values", "sheets_append_or_replace"}
    assert {n for n, t in tools.items() if t.source} == {"gmail_poll", "chat_poll", "calendar_poll"}
