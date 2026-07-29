from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import reply_server
from db_manager import DBManager


USER = {"user_id": 1, "username": "owner"}
OTHER_USER = {"user_id": 2, "username": "other"}
CARD = {"id": 7, "name": "测试商品", "type": "text", "user_id": 1}


@pytest.fixture()
def api_state(tmp_path, monkeypatch):
    manager = DBManager(str(tmp_path / "task5.sqlite3"))
    monkeypatch.setattr(reply_server, "db_manager", manager)
    monkeypatch.setattr(
        manager,
        "get_all_cookies",
        lambda user_id=None: {"cookie-a": "masked-cookie"} if user_id == 1 else {},
    )
    monkeypatch.setattr(
        manager,
        "get_card_by_id",
        lambda card_id, user_id=None: CARD if int(card_id) == 7 and user_id == 1 else None,
    )
    yield manager
    manager.close()


def _delivery_request(mode, config):
    return reply_server.DeliveryConfigRequest(mode=mode, config=config)


def _put_config(config, *, user=USER, card_id=7, account_id="cookie-a"):
    return reply_server.put_delivery_config(
        card_id=card_id,
        account_id=account_id,
        request=_delivery_request(config["mode"], config["config"]),
        current_user=user,
    )


def test_task5_routes_are_present_and_authenticated():
    expected = {
        ("/api/cards/{card_id}/delivery-config", ("DELETE",)),
        ("/api/cards/{card_id}/delivery-config", ("GET",)),
        ("/api/cards/{card_id}/delivery-config", ("PUT",)),
        ("/api/cards/{card_id}/inventory/settings", ("GET",)),
        ("/api/cards/{card_id}/inventory/settings", ("PUT",)),
        ("/api/cards/{card_id}/inventory", ("GET",)),
        ("/api/cards/{card_id}/inventory/import", ("POST",)),
        ("/api/cards/{card_id}/inventory/generate", ("POST",)),
        ("/api/cards/{card_id}/inventory/preview", ("GET",)),
    }
    actual = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in reply_server.app.routes
        if "/delivery-config" in getattr(route, "path", "")
        or "/inventory" in getattr(route, "path", "")
    }
    assert expected <= actual
    for route in reply_server.app.routes:
        route_path = getattr(route, "path", "")
        if route_path in {item[0] for item in expected}:
            assert any(
                dependency.call is reply_server.get_current_user
                for dependency in route.dependant.dependencies
            )


@pytest.mark.parametrize(
    "mode, config, marker",
    [
        ("fixed_link", {"url": "https://private.example/s/secret?pwd=abc"}, "private.example"),
        ("imported_card", {"source": "local-import"}, "local-import"),
        ("generated_card", {"source": "local-generated"}, "local-generated"),
        (
            "provider_api",
            {"endpoint": "https://provider.example/send", "api_key": "provider-secret"},
            "provider-secret",
        ),
    ],
)
def test_delivery_config_crud_returns_only_safe_summary(api_state, mode, config, marker):
    saved = _put_config({"mode": mode, "config": config})

    assert saved["mode"] == mode
    assert saved["config_summary"]["configured"] is True
    assert marker not in json.dumps(saved, ensure_ascii=False)

    fetched = reply_server.get_delivery_config(7, "cookie-a", USER)
    assert fetched["mode"] == mode
    assert "config" not in fetched
    assert marker not in json.dumps(fetched, ensure_ascii=False)

    deleted = reply_server.delete_delivery_config(7, "cookie-a", USER)
    assert deleted == {"deleted": True, "card_id": 7, "account_id": "cookie-a"}


def test_delivery_config_rejects_empty_unknown_mode_and_non_http_link(api_state):
    cases = [
        {"mode": "", "config": {"source": "x"}},
        {"mode": "unknown", "config": {"source": "x"}},
        {"mode": "fixed_link", "config": {"url": "ftp://private.example/file"}},
        {"mode": "fixed_link", "config": {}},
    ]
    for payload in cases:
        with pytest.raises(HTTPException) as error:
            _put_config(payload)
        assert error.value.status_code == 400
        assert error.value.detail["code"] in {"invalid_mode", "invalid_config"}


def test_delivery_config_rejects_cross_user_and_cross_account_scope(api_state):
    with pytest.raises(HTTPException) as other_user:
        _put_config(
            {"mode": "imported_card", "config": {"source": "local"}},
            user=OTHER_USER,
        )
    assert other_user.value.status_code == 403

    with pytest.raises(HTTPException) as other_account:
        _put_config(
            {"mode": "imported_card", "config": {"source": "local"}},
            account_id="cookie-other",
        )
    assert other_account.value.status_code == 403

    with pytest.raises(HTTPException) as other_card:
        _put_config(
            {"mode": "imported_card", "config": {"source": "local"}},
            card_id=99,
        )
    assert other_card.value.status_code == 403


def test_inventory_api_delegates_settings_import_generate_summary_and_masked_preview(
    api_state, caplog
):
    settings = reply_server.update_inventory_settings(
        card_id=7,
        account_id="cookie-a",
        request=reply_server.InventorySettingsRequest(stock_ceiling=3),
        current_user=USER,
    )
    assert settings["settings"]["stock_ceiling"] == 3

    imported = reply_server.import_card_inventory(
        card_id=7,
        account_id="cookie-a",
        request=reply_server.InventoryImportRequest(
            secrets=["manual-secret-a", "manual-secret-b", "manual-secret-a", ""]
        ),
        current_user=USER,
    )
    assert imported["inserted"] == 2
    assert imported["duplicates"] == 1
    assert imported["blank"] == 1
    assert "manual-secret-a" not in json.dumps(imported, ensure_ascii=False)
    assert "manual-secret-a" not in caplog.text

    generated = reply_server.generate_card_inventory(7, "cookie-a", USER)
    assert generated["generated"] == 1
    assert "manual-secret" not in json.dumps(generated, ensure_ascii=False)

    summary = reply_server.get_card_inventory(7, "cookie-a", USER)
    assert summary["inventory"]["available"] == 3
    assert summary["inventory"]["reserved"] == 0
    assert summary["inventory"]["sent"] == 0
    assert summary["inventory"]["invalidated"] == 0
    assert summary["inventory"]["shortage"] == 0

    preview = reply_server.preview_card_inventory(7, "cookie-a", USER)
    assert preview["items"]
    assert all("manual-secret" not in item for item in preview["items"])
    assert all("*" in item for item in preview["items"])


def test_inventory_api_rejects_cross_user_scope(api_state):
    with pytest.raises(HTTPException) as error:
        reply_server.get_card_inventory(7, "cookie-a", OTHER_USER)
    assert error.value.status_code == 403


def test_setup_delivery_summary_includes_task5_config_without_leaking_config(api_state, monkeypatch):
    class EmptyStore:
        def list_templates(self, cookie_id):
            return []

    monkeypatch.setattr(reply_server, "_get_republish_store", lambda: EmptyStore())
    monkeypatch.setattr(
        api_state,
        "get_cookie_details",
        lambda cookie_id: {"user_id": 1} if cookie_id == "cookie-a" else None,
    )
    _put_config(
        {
            "mode": "fixed_link",
            "config": {"url": "https://private.example/s/secret?pwd=abc"},
        }
    )

    summary = reply_server._get_guided_delivery_summary("cookie-a")
    assert summary["configured"] is True
    assert summary["config_count"] == 1
    assert "private.example" not in json.dumps(summary, ensure_ascii=False)
