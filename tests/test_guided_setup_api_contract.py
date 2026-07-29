import pytest
from fastapi.testclient import TestClient

import reply_server


USER = {"user_id": 7, "username": "guided-user", "is_admin": False}


@pytest.fixture
def client():
    with TestClient(reply_server.app) as test_client:
        yield test_client


@pytest.fixture
def authenticated_client():
    reply_server.app.dependency_overrides[reply_server.get_current_user] = lambda: USER
    try:
        with TestClient(reply_server.app) as test_client:
            yield test_client
    finally:
        reply_server.app.dependency_overrides.pop(reply_server.get_current_user, None)


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
    assert payload["accounts"][0]["guided_status"]["step_id"] == "account_connected"
    assert "cookie-secret" not in response.text
    assert "token-secret" not in response.text
    assert "password-secret" not in response.text


def test_setup_action_rejects_unknown_action_and_keeps_allowed_actions(authenticated_client, monkeypatch):
    monkeypatch.setattr(reply_server, "_get_user_cookies_map", lambda current_user: {"account-1": "masked"})

    unknown = authenticated_client.post("/setup/action", json={"action": "delete_everything"})
    assert unknown.status_code == 400

    for action in ("refresh_status", "go_to_delivery_config", "finish"):
        response = authenticated_client.post("/setup/action", json={"action": action})
        assert response.status_code == 200
        assert response.json()["action"] == action


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
