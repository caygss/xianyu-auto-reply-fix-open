from pathlib import Path
from types import SimpleNamespace
import asyncio

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


def test_subsecond_before_deadline_keeps_status_and_password_login_blocked(monkeypatch):
    live = _fake_live_instance()
    backoff = {
        "reason": "slider_failed",
        "until": 1001,
        "seconds": 600,
    }
    _patch_runtime(monkeypatch, live, backoff, 1000.1)

    runtime_status = reply_server._build_live_runtime_status("account-1")

    assert runtime_status["token_refresh_backoff_until"] == 1001
    assert runtime_status["token_refresh_remaining_seconds"] == 1
    assert runtime_status["token_refresh_can_retry"] is False
    assert runtime_status["user_action"] == "wait_backoff"

    reply_server.password_login_sessions.clear()
    monkeypatch.setattr(
        reply_server.db_manager,
        "get_cookie_details",
        lambda cid: {
            "user_id": 7,
            "username": "demo-user",
            "password": "demo-password",
            "show_browser": True,
        },
    )
    created_tasks = []
    monkeypatch.setattr(reply_server, "_execute_password_login", lambda *args: created_tasks.append(args))
    monkeypatch.setattr(
        reply_server.asyncio,
        "create_task",
        lambda coroutine: created_tasks.append(coroutine) or object(),
    )

    response = asyncio.run(
        reply_server.password_login(
            {
                "account_id": "account-1",
                "refresh_mode": True,
                "show_browser": True,
            },
            {"user_id": 7, "username": "test-user"},
        )
    )

    assert response["success"] is False
    assert response["token_refresh_remaining_seconds"] == 1
    assert response["token_refresh_can_retry"] is False
    assert response["user_action"] == "wait_backoff"
    assert "1" in response["message"]
    assert created_tasks == []
    assert reply_server.password_login_sessions == {}


def test_float_token_deadline_is_preserved_and_blocks_before_expiry(monkeypatch):
    live = _fake_live_instance()
    backoff = {
        "reason": "slider_failed",
        "until": 1600.9,
        "seconds": 600,
    }
    _patch_runtime(monkeypatch, live, backoff, 1600.1)

    runtime_status = reply_server._build_live_runtime_status("account-1")

    assert runtime_status["token_refresh_backoff_until"] == 1600.9
    assert runtime_status["token_refresh_remaining_seconds"] == 1
    assert runtime_status["token_refresh_can_retry"] is False
    assert runtime_status["user_action"] == "wait_backoff"

    reply_server.password_login_sessions.clear()
    monkeypatch.setattr(
        reply_server.db_manager,
        "get_cookie_details",
        lambda cid: {
            "user_id": 7,
            "username": "demo-user",
            "password": "demo-password",
            "show_browser": True,
        },
    )
    created_tasks = []
    monkeypatch.setattr(reply_server, "_execute_password_login", lambda *args: created_tasks.append(args))
    monkeypatch.setattr(
        reply_server.asyncio,
        "create_task",
        lambda coroutine: created_tasks.append(coroutine) or object(),
    )

    response = asyncio.run(
        reply_server.password_login(
            {"account_id": "account-1", "refresh_mode": True, "show_browser": True},
            {"user_id": 7, "username": "test-user"},
        )
    )

    assert response["success"] is False
    assert response["token_refresh_backoff_until"] == 1600.9
    assert response["token_refresh_remaining_seconds"] == 1
    assert response["token_refresh_can_retry"] is False
    assert response["user_action"] == "wait_backoff"
    assert created_tasks == []
    assert reply_server.password_login_sessions == {}


def test_float_qr_grace_deadline_is_preserved_and_blocks_before_expiry(monkeypatch):
    live = _fake_live_instance(status=None)
    _patch_runtime(monkeypatch, live, None, 1600.1)
    monkeypatch.setattr(
        reply_server.db_manager,
        "get_cookie_details",
        lambda cid: {"qr_login_grace_until": 1600.9},
    )

    runtime_status = reply_server._build_live_runtime_status("account-1")

    assert runtime_status["qr_login_grace_until"] == 1600.9
    assert runtime_status["token_refresh_remaining_seconds"] == 1
    assert runtime_status["token_refresh_can_retry"] is False
    assert runtime_status["user_action"] == "wait_backoff"
    assert runtime_status["token_refresh_status"] == "qr_login_grace_wait"


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
    assert "qr_login_grace_until" in source
    assert "getRuntimeDeadline" in source
    assert "自动验证失败，当前需要等待" in source
    assert "账号正在稳定，当前需要等待" in source
    assert "Math.ceil" in source
    assert "manual_browser_session_status" in source


def test_password_login_rejects_backoff_before_creating_browser_task(monkeypatch):
    reply_server.password_login_sessions.clear()
    monkeypatch.setattr(
        reply_server.db_manager,
        "get_cookie_details",
        lambda cid: {
            "user_id": 7,
            "username": "demo-user",
            "password": "demo-password",
            "show_browser": True,
        },
    )
    monkeypatch.setattr(reply_server.time, "time", lambda: 1000)
    monkeypatch.setattr(
        XianyuLive,
        "get_password_login_failure_backoff",
        classmethod(lambda cls, cid: {"reason": "slider_failed", "until": 1600}),
    )
    monkeypatch.setattr(
        XianyuLive,
        "is_manual_refresh_active",
        classmethod(lambda cls, cid, allow_handoff_recovery=False: False),
    )
    created_tasks = []
    monkeypatch.setattr(reply_server, "_execute_password_login", lambda *args: created_tasks.append(args))
    monkeypatch.setattr(
        reply_server.asyncio,
        "create_task",
        lambda coroutine: created_tasks.append(coroutine) or object(),
    )

    response = asyncio.run(
        reply_server.password_login(
            {
                "account_id": "account-1",
                "refresh_mode": True,
                "show_browser": True,
            },
            {"user_id": 7, "username": "test-user"},
        )
    )

    assert response["success"] is False
    assert response["token_refresh_backoff_reason"] == "slider_failed"
    assert response["token_refresh_backoff_until"] == 1600
    assert response["token_refresh_remaining_seconds"] == 600
    assert response["token_refresh_can_retry"] is False
    assert response["user_action"] == "wait_backoff"
    assert "600" in response["message"]
    assert "分钟" in response["message"]
    assert created_tasks == []
    assert reply_server.password_login_sessions == {}


def test_automatic_password_recovery_backoff_sets_explicit_status_without_browser(monkeypatch):
    live = XianyuLive.__new__(XianyuLive)
    live.cookie_id = "account-1"
    live.last_token_refresh_status = None
    live.last_token_refresh_error_message = None
    live.last_password_login_backoff_log_time = 0
    live.is_manual_refresh_active = lambda *args, **kwargs: False
    live._is_account_pause_status = lambda status: False
    live._should_defer_auth_recovery_for_qr_grace = lambda *args, **kwargs: False
    live._has_recent_slider_success = lambda *args, **kwargs: False
    live._create_risk_log = lambda **kwargs: 1
    live._update_risk_log = lambda *args, **kwargs: None
    monkeypatch.setattr(reply_server.time, "time", lambda: 1000)
    monkeypatch.setattr(
        XianyuLive,
        "get_password_login_failure_backoff",
        classmethod(lambda cls, cid: {"reason": "slider_failed", "until": 1600}),
    )
    monkeypatch.setattr(
        XianyuLive,
        "consume_manual_refresh_slider_failed_bypass",
        classmethod(lambda cls, cid: False),
    )

    result = asyncio.run(live._try_password_login_refresh("Token 过期"))

    assert result is False
    assert live.last_token_refresh_status == "password_login_backoff_wait"
    assert "600" in live.last_token_refresh_error_message
