from pathlib import Path
from types import SimpleNamespace

import reply_server
from XianyuAutoAsync import XianyuLive


REPO_ROOT = Path(__file__).resolve().parents[1]


def _fake_live_instance(status="password_login_backoff_wait"):
    return SimpleNamespace(
        connection_state="connected",
        ws=None,
        session=None,
        current_token=None,
        last_token_refresh_status=status,
        last_token_refresh_error_message="旧的静态错误文案",
        last_token_refresh_time=0,
        last_session_keepalive_status=None,
        last_session_keepalive_time=0,
        last_heartbeat_response=0,
        last_heartbeat_time=0,
        last_successful_connection=0,
        last_state_change_time=0,
        cookie_refresh_enabled=True,
    )


def _patch_runtime(monkeypatch, live, backoff_state, now):
    monkeypatch.setattr(reply_server.time, "time", lambda: now)
    monkeypatch.setattr(reply_server.db_manager, "get_cookie_details", lambda cid: {})
    monkeypatch.setattr(
        reply_server.cookie_manager,
        "manager",
        SimpleNamespace(live_instances={"account-1": live}),
    )
    monkeypatch.setattr(XianyuLive, "get_password_login_failure_backoff", classmethod(lambda cls, cid: backoff_state))
    monkeypatch.setattr(XianyuLive, "get_auth_recovery_lock_state", classmethod(lambda cls, cid: None))
    monkeypatch.setattr(XianyuLive, "is_manual_refresh_active", classmethod(lambda cls, cid, allow_handoff_recovery=False: False))


def test_runtime_status_uses_backend_deadline_and_exposes_retry_contract(monkeypatch):
    live = _fake_live_instance()
    backoff = {
        "reason": "slider_failed",
        "until": 130,
        "seconds": 600,
        "created_at": 100,
    }

    _patch_runtime(monkeypatch, live, backoff, 100)
    first = reply_server._build_live_runtime_status("account-1")

    _patch_runtime(monkeypatch, live, backoff, 110)
    second = reply_server._build_live_runtime_status("account-1")

    required_fields = {
        "token_refresh_backoff_reason",
        "token_refresh_backoff_until",
        "token_refresh_remaining_seconds",
        "token_refresh_can_retry",
        "user_action",
    }
    assert required_fields <= first.keys()
    assert first["token_refresh_backoff_reason"] == "slider_failed"
    assert first["token_refresh_backoff_until"] == 130
    assert first["token_refresh_remaining_seconds"] == 30
    assert second["token_refresh_remaining_seconds"] == 20
    assert first["token_refresh_can_retry"] is False
    assert first["user_action"] == "wait_backoff"


def test_expired_backend_deadline_reenables_retry_without_static_error_message(monkeypatch):
    live = _fake_live_instance()
    _patch_runtime(monkeypatch, live, None, 131)

    status = reply_server._build_live_runtime_status("account-1")

    assert status["token_refresh_remaining_seconds"] == 0
    assert status["token_refresh_can_retry"] is True
    assert status["user_action"] == "open_manual_verification"


def test_frontend_contract_uses_deadline_for_chinese_backoff_countdown():
    source = (REPO_ROOT / "static/js/app.js").read_text(encoding="utf-8")

    assert "token_refresh_backoff_until" in source
    assert "自动验证失败，当前需要等待" in source
    assert "Math.ceil" in source
    assert "manual_browser_session_status" in source
