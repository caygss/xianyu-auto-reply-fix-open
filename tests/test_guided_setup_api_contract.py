import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace

import reply_server


USER = {"user_id": 7, "username": "guided-user", "is_admin": False}


@pytest.fixture
def client():
    with TestClient(reply_server.app) as test_client:
        yield test_client


@pytest.fixture
def authenticated_client():
    previous = reply_server.app.dependency_overrides.get(reply_server.get_current_user)
    reply_server.app.dependency_overrides[reply_server.get_current_user] = lambda: USER
    try:
        with TestClient(reply_server.app) as test_client:
            yield test_client
    finally:
        if previous is None:
            reply_server.app.dependency_overrides.pop(reply_server.get_current_user, None)
        else:
            reply_server.app.dependency_overrides[reply_server.get_current_user] = previous
        reply_server.guided_manual_verification_actions.clear()


def test_guided_setup_routes_are_registered():
    routes = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in reply_server.app.routes
        if getattr(route, "path", "").startswith("/setup")
        or getattr(route, "path", "").startswith("/cookies/{cid}/manual-verification")
    }

    assert ("/setup/status", ("GET",)) in routes
    assert ("/setup/action", ("POST",)) in routes
    assert ("/cookies/{cid}/manual-verification/open", ("POST",)) in routes
    assert ("/cookies/{cid}/manual-verification/complete", ("POST",)) in routes


@pytest.mark.parametrize(
    ("method", "path", "json"),
    [
        ("get", "/setup/status", None),
        ("post", "/setup/action", {"action": "refresh_status"}),
        ("post", "/cookies/account-1/manual-verification/open", {}),
        ("post", "/cookies/account-1/manual-verification/complete", {}),
    ],
)
def test_guided_setup_routes_require_login(client, method, path, json):
    response = getattr(client, method)(path, json=json) if json is not None else getattr(client, method)(path)

    assert response.status_code == 401


def test_setup_status_returns_safe_guided_json_and_reuses_cookie_details(authenticated_client, monkeypatch):
    runtime_status = {
        "connection_state": "connected",
        "running": True,
        "message_stream_ready": True,
        "token_refresh_status": "success",
        "current_token": "token-secret",
    }
    details = [
        {
            "id": "account-1",
            "value": "cookie-secret",
            "password": "password-secret",
            "runtime_status": runtime_status,
        }
    ]
    calls = []

    def fake_details(current_user):
        calls.append(current_user)
        return details

    monkeypatch.setattr(reply_server, "get_cookies_details", fake_details)

    response = authenticated_client.get("/setup/status")

    assert response.status_code == 200
    payload = response.json()
    assert calls == [USER]
    assert payload["success"] is True
    assert payload["accounts"][0]["cookie_id"] == "account-1"
    assert payload["accounts"][0]["runtime_ready"] is True
    assert payload["accounts"][0]["guided_status"]["step_id"] == "delivery_config"
    assert "cookie-secret" not in response.text
    assert "token-secret" not in response.text
    assert "password-secret" not in response.text


def test_setup_status_reuses_real_configured_delivery_summary(authenticated_client, monkeypatch):
    captured = []
    summary_calls = []
    original_build_guided_status = reply_server.build_guided_status

    def spy_build_guided_status(runtime_status, account_details=None, delivery_summary=None):
        captured.append(delivery_summary)
        return original_build_guided_status(runtime_status, account_details, delivery_summary)

    monkeypatch.setattr(reply_server, "build_guided_status", spy_build_guided_status)
    monkeypatch.setattr(
        reply_server,
        "_get_guided_delivery_summary",
        lambda cookie_id: summary_calls.append(cookie_id) or {"configured": True, "template_count": 1},
    )
    monkeypatch.setattr(
        reply_server,
        "get_cookies_details",
        lambda current_user: [{
            "id": "account-1",
            "runtime_status": {"connection_state": "connected", "running": True, "message_stream_ready": True},
        }],
    )

    response = authenticated_client.get("/setup/status")

    assert response.status_code == 200
    assert summary_calls == ["account-1"]
    assert captured == [{"configured": True, "template_count": 1}]
    guided_status = response.json()["accounts"][0]["guided_status"]
    assert guided_status["primary_action"] == "finish"
    assert guided_status["step_index"] == 6
    assert response.json()["accounts"][0]["runtime_ready"] is True


def test_setup_status_keeps_unconfigured_account_before_completion(authenticated_client, monkeypatch):
    monkeypatch.setattr(
        reply_server,
        "_get_guided_delivery_summary",
        lambda cookie_id: {"configured": False, "template_count": 0},
    )
    monkeypatch.setattr(
        reply_server,
        "get_cookies_details",
        lambda current_user: [{
            "id": "account-1",
            "runtime_status": {"connection_state": "connected", "running": True, "message_stream_ready": True},
        }],
    )

    response = authenticated_client.get("/setup/status")

    assert response.status_code == 200
    guided_status = response.json()["accounts"][0]["guided_status"]
    assert guided_status["primary_action"] == "go_to_delivery_config"
    assert guided_status["step_index"] == 5


@pytest.mark.parametrize(
    "runtime_status",
    [
        {"connection_state": "connected", "message_stream_status": "recovering", "message_stream_ready": True},
        {"connection_state": "connected", "message_stream_status": "SUSPECTED_STALE", "message_stream_ready": "true"},
        {"connection_state": "connected", "message_stream_status": "HEALTHY", "message_stream_ready": "FALSE"},
        {"connection_state": "connected", "message_stream_status": "healthy"},
        {"status": "CONNECTED", "message_stream_status": "healthy"},
    ],
)
def test_setup_status_does_not_finish_configured_account_with_unready_message_stream(
    authenticated_client, monkeypatch, runtime_status
):
    monkeypatch.setattr(
        reply_server,
        "_get_guided_delivery_summary",
        lambda cookie_id: {"configured": True, "template_count": 1},
    )
    monkeypatch.setattr(
        reply_server,
        "get_cookies_details",
        lambda current_user: [{
            "id": "account-1",
            "runtime_status": runtime_status,
        }],
    )

    response = authenticated_client.get("/setup/status")

    assert response.status_code == 200
    guided_status = response.json()["accounts"][0]["guided_status"]
    assert guided_status["primary_action"] == "refresh_status"
    assert guided_status["step_index"] == 5
    assert guided_status["needs_user_action"] is False
    assert guided_status["technical_status"] == "connection_unready"
    assert response.json()["accounts"][0]["runtime_ready"] is False


def test_setup_action_rejects_unknown_action_and_keeps_allowed_actions(authenticated_client, monkeypatch):
    monkeypatch.setattr(reply_server, "_get_user_cookies_map", lambda current_user: {"account-1": "masked"})

    unknown = authenticated_client.post("/setup/action", json={"action": "delete_everything"})
    assert unknown.status_code == 400

    for action in ("refresh_status", "go_to_delivery_config"):
        response = authenticated_client.post("/setup/action", json={"action": action})
        assert response.status_code == 200
        assert response.json()["action"] == action

    finish_without_cookie = authenticated_client.post("/setup/action", json={"action": "finish"})
    assert finish_without_cookie.status_code == 400


def test_setup_finish_is_blocked_until_account_is_running_and_delivery_is_configured(
    authenticated_client, monkeypatch
):
    monkeypatch.setattr(reply_server, "_get_user_cookies_map", lambda current_user: {"account-1": "masked"})
    monkeypatch.setattr(
        reply_server,
        "_get_republish_store",
        lambda: SimpleNamespace(list_templates=lambda cookie_id: []),
    )
    monkeypatch.setattr(
        reply_server,
        "_build_live_runtime_status",
        lambda cid: {"connection_state": "connected", "running": True, "message_stream_ready": True},
    )

    response = authenticated_client.post("/setup/action", json={"action": "finish", "cookie_id": "account-1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["next_action"] == "go_to_delivery_config"
    assert payload["guided_status"]["primary_action"] == "go_to_delivery_config"


def test_setup_finish_uses_configured_delivery_summary_and_can_complete(authenticated_client, monkeypatch):
    monkeypatch.setattr(reply_server, "_get_user_cookies_map", lambda current_user: {"account-1": "masked"})
    monkeypatch.setattr(
        reply_server,
        "_get_republish_store",
        lambda: SimpleNamespace(
            list_templates=lambda cookie_id: [
                SimpleNamespace(
                    auto_delivery=True,
                    delivery_content="delivery-secret",
                    sku_delivery={},
                    delivery_choice="digital",
                )
            ]
        ),
    )
    monkeypatch.setattr(
        reply_server,
        "_build_live_runtime_status",
        lambda cid: {"connection_state": "connected", "running": True, "message_stream_ready": True},
    )

    response = authenticated_client.post("/setup/action", json={"action": "finish", "cookie_id": "account-1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["guided_status"]["primary_action"] == "finish"
    assert "delivery-secret" not in response.text


@pytest.mark.parametrize(
    "runtime_status",
    [
        {},
        {"connection_state": "connected"},
        {"connection_state": "connected", "message_stream_status": "recovering", "message_stream_ready": True},
        {"connection_state": "connected", "message_stream_status": "suspected_stale", "message_stream_ready": True},
        {"connection_state": "connected", "message_stream_ready": "FALSE"},
        {"connection_state": "connected", "message_stream_ready": True},
        {"connection_state": "connected", "running": False, "message_stream_ready": True},
    ],
)
def test_setup_finish_fails_closed_when_runtime_readiness_is_missing_or_unready(
    authenticated_client, monkeypatch, runtime_status
):
    monkeypatch.setattr(reply_server, "_get_user_cookies_map", lambda current_user: {"account-1": "masked"})
    monkeypatch.setattr(
        reply_server,
        "_get_republish_store",
        lambda: SimpleNamespace(
            list_templates=lambda cookie_id: [
                SimpleNamespace(
                    auto_delivery=True,
                    delivery_content="delivery-secret",
                    sku_delivery={},
                    delivery_choice="digital",
                )
            ]
        ),
    )
    monkeypatch.setattr(reply_server, "_build_live_runtime_status", lambda cid: runtime_status)

    response = authenticated_client.post("/setup/action", json={"action": "finish", "cookie_id": "account-1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["next_action"] == "refresh_status"
    assert payload["guided_status"]["primary_action"] == "refresh_status"


def test_setup_finish_is_blocked_when_account_is_not_running(authenticated_client, monkeypatch):
    monkeypatch.setattr(reply_server, "_get_user_cookies_map", lambda current_user: {"account-1": "masked"})
    monkeypatch.setattr(
        reply_server,
        "_build_live_runtime_status",
        lambda cid: {"connection_state": "reconnecting"},
    )

    response = authenticated_client.post("/setup/action", json={"action": "finish", "cookie_id": "account-1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["next_action"] == "refresh_status"


def test_setup_finish_is_blocked_when_runtime_marks_account_not_running(authenticated_client, monkeypatch):
    monkeypatch.setattr(reply_server, "_get_user_cookies_map", lambda current_user: {"account-1": "masked"})
    monkeypatch.setattr(
        reply_server,
        "_build_live_runtime_status",
        lambda cid: {
            "running": False,
            "connection_state": "connected",
            "message_stream_ready": True,
        },
    )

    response = authenticated_client.post("/setup/action", json={"action": "finish", "cookie_id": "account-1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["next_action"] == "refresh_status"


@pytest.mark.parametrize("path", [
    "/cookies/account-1/manual-verification/open",
    "/cookies/account-1/manual-verification/complete",
])
def test_manual_verification_actions_use_cookie_access_check(authenticated_client, monkeypatch, path):
    calls = []

    def ensure_access(cid, current_user):
        calls.append((cid, current_user))
        return cid

    monkeypatch.setattr(reply_server, "_ensure_cookie_access", ensure_access)

    response = authenticated_client.post(path, json={})

    assert response.status_code == 200
    assert calls == [("account-1", USER)]
    assert response.json()["cookie_id"] == "account-1"


def test_manual_verification_actions_record_pending_state_and_next_action(authenticated_client, monkeypatch):
    reply_server.guided_manual_verification_actions.clear()
    monkeypatch.setattr(
        reply_server,
        "_build_live_runtime_status",
        lambda cid: {
            "token_refresh_status": "verification_pending_manual",
            "connection_state": "reconnecting",
        },
    )
    monkeypatch.setattr(reply_server, "_ensure_cookie_access", lambda cid, user: cid)

    opened = authenticated_client.post("/cookies/account-1/manual-verification/open", json={})
    assert opened.status_code == 200
    assert opened.json()["status"] == "pending"
    assert opened.json()["next_action"] == "complete_manual_verification"
    assert opened.json()["guided_status"]["primary_action"] == "complete_manual_verification"
    assert reply_server.guided_manual_verification_actions["account-1"]["status"] == "open_pending"

    monkeypatch.setattr(
        reply_server,
        "get_cookies_details",
        lambda current_user: [{
            "id": "account-1",
            "runtime_status": {"token_refresh_status": "verification_pending_manual"},
        }],
    )
    setup_status = authenticated_client.get("/setup/status")
    assert setup_status.status_code == 200
    assert setup_status.json()["accounts"][0]["guided_status"]["primary_action"] == "complete_manual_verification"

    completed = authenticated_client.post("/cookies/account-1/manual-verification/complete", json={})
    assert completed.status_code == 200
    assert completed.json()["status"] == "pending"
    assert completed.json()["next_action"] == "refresh_status"
    assert completed.json()["guided_status"]["primary_action"] == "refresh_status"
    assert reply_server.guided_manual_verification_actions["account-1"]["status"] == "complete_pending"


def test_manual_verification_cross_account_access_is_denied(authenticated_client, monkeypatch):
    monkeypatch.setattr(reply_server, "_get_user_cookies_map", lambda current_user: {"own-account": "masked"})

    response = authenticated_client.post("/cookies/other-account/manual-verification/open", json={})

    assert response.status_code == 403
    assert "secret" not in response.text


def test_manual_verification_unexpected_error_uses_safe_client_message(authenticated_client, monkeypatch):
    monkeypatch.setattr(reply_server, "_ensure_cookie_access", lambda cid, user: cid)
    monkeypatch.setattr(
        reply_server,
        "_build_live_runtime_status",
        lambda cid: (_ for _ in ()).throw(RuntimeError("password=secret https://private.example")),
    )

    response = authenticated_client.post("/cookies/account-1/manual-verification/open", json={})

    assert response.status_code == 400
    assert "secret" not in response.text
    assert "private.example" not in response.text


def test_manual_verification_build_failure_rolls_back_new_pending_state(authenticated_client, monkeypatch):
    reply_server.guided_manual_verification_actions.clear()
    monkeypatch.setattr(reply_server, "_ensure_cookie_access", lambda cid, user: cid)
    monkeypatch.setattr(
        reply_server,
        "_build_live_runtime_status",
        lambda cid: (_ for _ in ()).throw(RuntimeError("runtime unavailable")),
    )

    response = authenticated_client.post("/cookies/account-1/manual-verification/open", json={})

    assert response.status_code == 400
    assert "account-1" not in reply_server.guided_manual_verification_actions

    monkeypatch.setattr(
        reply_server,
        "_build_live_runtime_status",
        lambda cid: {"token_refresh_status": "verification_pending_manual", "connection_state": "reconnecting"},
    )
    recovered = authenticated_client.post("/cookies/account-1/manual-verification/open", json={})

    assert recovered.status_code == 200
    assert reply_server.guided_manual_verification_actions["account-1"]["status"] == "open_pending"


def test_manual_verification_build_failure_preserves_previous_pending_state(authenticated_client, monkeypatch):
    previous_state = {
        "status": "open_pending",
        "action": "open_manual_verification",
        "updated_at": 123.0,
    }
    reply_server.guided_manual_verification_actions["account-1"] = dict(previous_state)
    monkeypatch.setattr(reply_server, "_ensure_cookie_access", lambda cid, user: cid)
    monkeypatch.setattr(
        reply_server,
        "_build_live_runtime_status",
        lambda cid: (_ for _ in ()).throw(RuntimeError("runtime unavailable")),
    )

    response = authenticated_client.post("/cookies/account-1/manual-verification/complete", json={})

    assert response.status_code == 400
    assert reply_server.guided_manual_verification_actions["account-1"] == previous_state


def test_live_runtime_exposes_real_grace_and_backoff_deadlines(monkeypatch):
    from XianyuAutoAsync import XianyuLive

    now = 1000
    monkeypatch.setattr(reply_server.cookie_manager, "manager", None)
    monkeypatch.setattr(reply_server.db_manager, "get_cookie_details", lambda cid: {"qr_login_grace_until": 1020})
    monkeypatch.setattr(XianyuLive, "get_instance", lambda cid: None)
    monkeypatch.setattr(XianyuLive, "get_auth_recovery_lock_state", lambda cid: None)
    monkeypatch.setattr(
        XianyuLive,
        "get_password_login_failure_backoff",
        lambda cid: {"until": 1030, "reason": "slider_failed"},
    )
    monkeypatch.setattr(reply_server.time, "time", lambda: now)

    status = reply_server._build_live_runtime_status("account-1")

    assert status["qr_login_grace_until"] == 1020
    assert status["token_refresh_backoff_until"] == 1030
    assert status["token_refresh_remaining_seconds"] == 30

    monkeypatch.setattr(XianyuLive, "get_password_login_failure_backoff", lambda cid: None)
    qr_only_status = reply_server._build_live_runtime_status("account-1")
    assert qr_only_status["token_refresh_status"] == "qr_login_grace_wait"
    assert qr_only_status["token_refresh_remaining_seconds"] == 20
