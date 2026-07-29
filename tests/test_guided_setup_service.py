from datetime import datetime, timezone
import math

import pytest

from guided_setup_service import (
    build_guided_status,
    format_remaining_seconds,
    get_user_action_for_runtime,
)


REQUIRED_FIELDS = {
    "step_id",
    "step_index",
    "step_total",
    "title",
    "message",
    "needs_user_action",
    "primary_action",
    "retry_at",
    "remaining_seconds",
    "technical_status",
    "technical_detail",
}


@pytest.mark.parametrize(
    ("runtime_status", "expected_action"),
    [
        ({"connection_state": "reconnecting"}, "refresh_status"),
        ({"token_refresh_status": "qr_login_grace_wait", "qr_login_grace_until": 130}, "refresh_status"),
        (
            {"token_refresh_status": "password_login_backoff_wait", "token_refresh_backoff_until": 130},
            "refresh_status",
        ),
        (
            {
                "token_refresh_status": "verification_pending_manual",
                "vnc_manual_action_available": True,
            },
            "open_manual_verification",
        ),
        ({"connection_state": "connected", "message_stream_ready": True}, "finish"),
    ],
)
def test_guided_status_exposes_stable_fields_and_safe_chinese_copy(runtime_status, expected_action):
    status = build_guided_status(
        runtime_status,
        account_details={
            "id": "account-1",
            "value": "cookie-secret",
            "password": "password-secret",
            "runtime_status": runtime_status,
        },
        delivery_summary={"configured": True},
    )

    assert REQUIRED_FIELDS <= status.keys()
    assert status["primary_action"] == expected_action
    assert status["technical_status"]
    assert "cookie-secret" not in repr(status)
    assert "password-secret" not in repr(status)
    for visible_text in (status["title"], status["message"]):
        assert all(term not in visible_text for term in ("WebSocket", "Token", "鉴权", "退避"))


def test_connected_account_with_delivery_ready_can_wait_for_order():
    status = build_guided_status(
        {"connection_state": "connected", "message_stream_ready": True},
        delivery_summary={"configured": True},
    )

    assert status["step_id"] == "ready_to_wait_for_order"
    assert status["step_index"] == status["step_total"]
    assert status["needs_user_action"] is False
    assert status["message"] == "账号已连接，交付配置已完成，现在可以等待买家下单。"


def test_connected_account_without_delivery_summary_must_go_to_delivery_config():
    status = build_guided_status({"connection_state": "connected"})

    assert status["step_id"] == "delivery_config"
    assert status["primary_action"] == "go_to_delivery_config"
    assert status["needs_user_action"] is True


@pytest.mark.parametrize(
    ("runtime_status", "expected_action"),
    [
        (
            {
                "connection_state": "connected",
                "token_refresh_status": "qr_login_grace_wait",
                "qr_login_grace_until": 130,
            },
            "refresh_status",
        ),
        (
            {
                "connection_state": "connected",
                "token_refresh_status": "verification_pending_manual",
                "manual_verification_open": True,
            },
            "complete_manual_verification",
        ),
        (
            {
                "connection_state": "connected",
                "token_refresh_status": "password_login_backoff_wait",
                "token_refresh_backoff_until": 130,
            },
            "refresh_status",
        ),
    ],
)
def test_explicit_wait_or_manual_action_has_priority_over_connected(runtime_status, expected_action, monkeypatch):
    monkeypatch.setattr("guided_setup_service.time.time", lambda: 100)

    status = build_guided_status(runtime_status, delivery_summary={"configured": True})

    assert status["primary_action"] == expected_action
    assert status["step_id"] != "ready_to_wait_for_order"


def test_active_runtime_deadline_has_priority_even_when_token_status_says_connected(monkeypatch):
    monkeypatch.setattr("guided_setup_service.time.time", lambda: 100)

    status = build_guided_status(
        {
            "connection_state": "connected",
            "token_refresh_status": "success",
            "qr_login_grace_until": 130,
        },
        delivery_summary={"configured": True},
    )

    assert status["primary_action"] == "refresh_status"
    assert status["technical_status"] == "qr_login_grace_wait"
    assert status["retry_at"] == 130
    assert status["remaining_seconds"] == 30


@pytest.mark.parametrize(
    "delivery_summary",
    [None, {}, {"configured": "false"}, {"configured": "0"}, {"configured": "off"}],
)
def test_false_like_delivery_values_are_not_treated_as_configured(delivery_summary):
    status = build_guided_status({"connection_state": "connected"}, delivery_summary=delivery_summary)

    assert status["primary_action"] == "go_to_delivery_config"
    assert status["step_id"] == "delivery_config"


@pytest.mark.parametrize(
    "runtime_status",
    [
        None,
        [],
        "not-a-runtime-mapping",
        {"token_refresh_status": "qr_login_grace_wait", "qr_login_grace_until": math.nan},
        {"token_refresh_status": "qr_login_grace_wait", "qr_login_grace_until": math.inf},
        {"token_refresh_status": "qr_login_grace_wait", "qr_login_grace_until": "not-a-deadline"},
    ],
)
def test_invalid_runtime_and_deadline_inputs_are_safe(runtime_status):
    status = build_guided_status(runtime_status)

    assert status["remaining_seconds"] == 0
    assert status["retry_at"] is None
    assert isinstance(status["technical_detail"], str)


def test_technical_detail_is_bounded_and_does_not_copy_upstream_error_text():
    status = build_guided_status(
        {
            "token_refresh_status": "https://example.test/?token=secret",
            "token_refresh_error_message": "password=secret https://example.test/private",
        }
    )

    assert len(status["technical_detail"]) <= 100
    assert "secret" not in status["technical_detail"]
    assert "example.test" not in status["technical_detail"]


def test_runtime_deadline_fields_drive_dynamic_remaining_seconds(monkeypatch):
    monkeypatch.setattr("guided_setup_service.time.time", lambda: 100)

    qr_status = build_guided_status(
        {"token_refresh_status": "qr_login_grace_wait", "qr_login_grace_until": 130}
    )
    backoff_status = build_guided_status(
        {
            "token_refresh_status": "password_login_backoff_wait",
            "token_refresh_backoff_until": 140,
        }
    )

    assert qr_status["retry_at"] == 130
    assert qr_status["remaining_seconds"] == 30
    assert backoff_status["retry_at"] == 140
    assert backoff_status["remaining_seconds"] == 40


def test_format_remaining_seconds_is_dynamic_and_never_negative():
    assert format_remaining_seconds(130, now=100) == 30
    assert format_remaining_seconds(99, now=100) == 0
    assert format_remaining_seconds(
        datetime.fromtimestamp(130, tz=timezone.utc),
        now=datetime.fromtimestamp(100, tz=timezone.utc),
    ) == 30
    assert format_remaining_seconds(None, now=100) == 0


def test_verification_status_can_switch_to_completion_action_after_opening():
    action = get_user_action_for_runtime(
        {
            "token_refresh_status": "verification_pending_manual",
            "manual_verification_open": True,
        }
    )

    assert action["action"] == "complete_manual_verification"
    assert action["needs_user_action"] is True
