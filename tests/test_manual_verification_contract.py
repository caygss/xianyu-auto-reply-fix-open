from pathlib import Path
from types import SimpleNamespace

import reply_server
from XianyuAutoAsync import XianyuLive


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_status_only_offers_browser_takeover_for_active_browser(monkeypatch):
    live = SimpleNamespace(
        connection_state="connected",
        ws=None,
        session=None,
        current_token=None,
        last_token_refresh_status="verification_pending_manual",
        last_token_refresh_error_message="验证失败",
        last_token_refresh_time=0,
        last_session_keepalive_status=None,
        last_session_keepalive_time=0,
        last_heartbeat_response=0,
        last_heartbeat_time=0,
        last_successful_connection=0,
        last_state_change_time=0,
        cookie_refresh_enabled=True,
    )
    monkeypatch.setattr(reply_server.time, "time", lambda: 100)
    monkeypatch.setattr(reply_server.db_manager, "get_cookie_details", lambda cid: {})
    monkeypatch.setattr(
        reply_server.cookie_manager,
        "manager",
        SimpleNamespace(live_instances={"account-1": live}),
    )
    monkeypatch.setattr(XianyuLive, "get_password_login_failure_backoff", classmethod(lambda cls, cid: None))
    monkeypatch.setattr(XianyuLive, "get_auth_recovery_lock_state", classmethod(lambda cls, cid: None))
    monkeypatch.setattr(XianyuLive, "is_manual_refresh_active", classmethod(lambda cls, cid, allow_handoff_recovery=False: False))

    status = reply_server._build_live_runtime_status("account-1")

    assert status["vnc_manual_action_available"] is False
    assert status["manual_browser_session_status"] is None


def test_manual_verification_contract_stops_automatic_reentry_and_clears_backoff_on_completion():
    source = (REPO_ROOT / "XianyuAutoAsync.py").read_text(encoding="utf-8")
    server_source = (REPO_ROOT / "reply_server.py").read_text(encoding="utf-8")

    assert "verification_pending_manual" in source
    assert "manual_verification_required" in source
    assert "XianyuLive.clear_password_login_failure_backoff" in server_source
    assert "complete_manual_verification" in server_source
    assert "token_refresh_backoff_reason" in server_source


def test_manual_verification_ui_does_not_fallback_to_status_as_browser_presence():
    source = (REPO_ROOT / "static/js/app.js").read_text(encoding="utf-8")

    assert "manual_browser_session_status" in source
    assert "return vncRelevantStatuses.has(tokenStatus);" not in source
