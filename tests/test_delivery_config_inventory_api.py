from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import reply_server
from card_inventory_service import CardInventoryService
from db_manager import DBManager


USER = {"user_id": 1, "username": "owner"}
OTHER_USER = {"user_id": 2, "username": "other"}
CARD = {"id": 7, "name": "测试商品", "type": "text", "user_id": 1}


@pytest.fixture()
def api_state(tmp_path, monkeypatch):
    manager = DBManager(str(tmp_path / "task5.sqlite3"))
    with manager.lock:
        manager.conn.execute(
            """
            INSERT INTO cards (id, name, type, description, user_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (CARD["id"], CARD["name"], CARD["type"], "普通卡券", CARD["user_id"]),
        )
        manager.conn.execute(
            "INSERT OR IGNORE INTO cookies (id, value, user_id) VALUES (?, ?, ?)",
            ("cookie-a", "cookie-a-value", USER["user_id"]),
        )
        manager.conn.execute(
            "INSERT OR IGNORE INTO cookies (id, value, user_id) VALUES (?, ?, ?)",
            ("cookie-b", "cookie-b-value", USER["user_id"]),
        )
        manager.conn.execute(
            "INSERT OR IGNORE INTO item_info (cookie_id, item_id, item_title) VALUES (?, ?, ?)",
            ("cookie-a", "item-100", "测试商品"),
        )
        manager.conn.commit()
    monkeypatch.setattr(reply_server, "db_manager", manager)
    monkeypatch.setattr(
        manager,
        "get_all_cookies",
        lambda user_id=None: (
            {"cookie-a": "masked-cookie", "cookie-b": "masked-cookie"}
            if user_id == 1
            else {}
        ),
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

    with pytest.raises(HTTPException) as malformed_ipv6:
        _put_config({"mode": "fixed_link", "config": {"url": "http://[::1"}})
    assert malformed_ipv6.value.status_code == 400
    assert malformed_ipv6.value.detail["code"] == "invalid_config"


@pytest.mark.parametrize(
    "url",
    [
        " https://example.com/path",
        "https://example.com/path ",
        "https://example.com/path\nnext",
        "https://example.com/path\x00next",
    ],
)
def test_fixed_link_rejects_whitespace_and_control_characters(api_state, url):
    with pytest.raises(HTTPException) as error:
        _put_config({"mode": "fixed_link", "config": {"url": url}})
    assert error.value.status_code == 400
    assert error.value.detail["code"] == "invalid_config"


@pytest.mark.parametrize(
    "url",
    [
        "https://:443/path",
        "https://example..com/path",
    ],
)
def test_fixed_link_rejects_invalid_hosts(api_state, url):
    with pytest.raises(HTTPException) as error:
        _put_config({"mode": "fixed_link", "config": {"url": url}})
    assert error.value.status_code == 400
    assert error.value.detail["code"] == "invalid_config"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:abc/path",
        "https://example.com:65536/path",
    ],
)
def test_fixed_link_rejects_invalid_ports(api_state, url):
    with pytest.raises(HTTPException) as error:
        _put_config({"mode": "fixed_link", "config": {"url": url}})
    assert error.value.status_code == 400
    assert error.value.detail["code"] == "invalid_config"


def test_fixed_link_rejects_extremely_long_port_without_server_error(api_state):
    url = "https://example.com:" + ("9" * 5000) + "/path"
    with pytest.raises(HTTPException) as error:
        _put_config({"mode": "fixed_link", "config": {"url": url}})
    assert error.value.status_code == 400
    assert error.value.detail["code"] == "invalid_config"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/%ZZ",
        "https://example.com/%",
        "https://example.com/%2",
    ],
)
def test_fixed_link_rejects_invalid_percent_encoding(api_state, url):
    with pytest.raises(HTTPException) as error:
        _put_config({"mode": "fixed_link", "config": {"url": url}})
    assert error.value.status_code == 400
    assert error.value.detail["code"] == "invalid_config"


def test_delivery_config_rejects_oversized_fixed_link_and_config_object(api_state):
    cases = [
        {
            "mode": "fixed_link",
            "config": {"url": "https://example.com/" + ("x" * 4096)},
        },
        {"mode": "imported_card", "config": {"source": "x" * 65536}},
    ]
    for payload in cases:
        with pytest.raises(HTTPException) as error:
            _put_config(payload)
        assert error.value.status_code == 400
        assert error.value.detail["code"] == "invalid_config"


@pytest.mark.parametrize(
    "model, payload",
    [
        (reply_server.DeliveryConfigRequest, {"mode": "imported_card", "config": {}, "extra": True}),
        (reply_server.InventorySettingsRequest, {"stock_ceiling": 3, "extra": True}),
        (reply_server.InventoryImportRequest, {"secrets": ["x"], "extra": True}),
    ],
)
def test_task5_request_models_reject_unknown_fields(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


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


@pytest.mark.parametrize("api_kind", ["config", "inventory"])
def test_internal_delivery_card_requires_its_bound_account(api_state, api_kind):
    internal = api_state.get_or_create_item_delivery_card(
        USER["user_id"], "cookie-a", "item-100", "测试商品"
    )

    def call(account_id):
        if api_kind == "config":
            return reply_server.put_delivery_config(
                internal["card_id"],
                account_id,
                _delivery_request("imported_card", {"source": "local"}),
                USER,
            )
        return reply_server.get_card_inventory(internal["card_id"], account_id, USER)

    with pytest.raises(HTTPException) as wrong_account:
        call("cookie-b")
    assert wrong_account.value.status_code == 403

    assert call("cookie-a")


def test_ordinary_card_keeps_cross_account_task5_behavior(api_state):
    saved = reply_server.put_delivery_config(
        CARD["id"],
        "cookie-b",
        _delivery_request("imported_card", {"source": "local"}),
        USER,
    )

    assert saved["card_id"] == CARD["id"]
    assert saved["account_id"] == "cookie-b"


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
    assert imported["shortage"] == 1
    assert imported["deficit"] == 1
    assert "manual-secret-a" not in json.dumps(imported, ensure_ascii=False)
    assert "manual-secret-a" not in caplog.text

    generated = reply_server.generate_card_inventory(7, "cookie-a", USER)
    assert generated["generated"] == 1
    assert generated["shortage"] == 0
    assert generated["deficit"] == 0
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


def test_inventory_service_always_encrypts_enc_prefixed_secret_and_reads_it_back(api_state):
    service = CardInventoryService(api_state)
    secret = "enc$looks-like-plaintext"

    service.import_items(7, 1, "cookie-a", [secret])

    stored = api_state.conn.execute(
        """
        SELECT secret_text FROM card_inventory_items
        WHERE user_id = 1 AND card_id = 7 AND account_id = 'cookie-a'
        """
    ).fetchone()[0]
    assert stored != secret
    assert stored.startswith("enc$")
    assert api_state._decrypt_secret(stored) == secret
    assert service.preview_items(7, 1, "cookie-a") == [service._mask_secret(secret)]


def test_inventory_api_rejects_oversized_import_inputs(api_state):
    oversized_cases = [
        ["x"] * 1001,
        ["x" * 4097],
        ["x" * 4096] * 300,
    ]
    for secrets in oversized_cases:
        with pytest.raises(HTTPException) as error:
            reply_server.import_card_inventory(
                card_id=7,
                account_id="cookie-a",
                request=reply_server.InventoryImportRequest(secrets=secrets),
                current_user=USER,
            )
        assert error.value.status_code == 400
        assert error.value.detail["code"] == "invalid_input"


def test_inventory_settings_round_trip_returns_complete_generator_configuration(api_state):
    saved = reply_server.update_inventory_settings(
        card_id=7,
        account_id="cookie-a",
        request=reply_server.InventorySettingsRequest(
            stock_ceiling=8,
            low_stock_threshold=2,
            auto_replenish=True,
            generator_prefix="AC-",
            generator_length=20,
            generator_charset="ABCDEFG234567",
        ),
        current_user=USER,
    )
    assert saved["settings"]["generator_prefix"] == "AC-"

    fetched = reply_server.get_inventory_settings(7, "cookie-a", USER)
    assert fetched["settings"] == saved["settings"]
    assert fetched["settings"]["updated_at"] == saved["settings"]["updated_at"]


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
    binding = api_state.get_or_create_item_delivery_card(
        USER["user_id"], "cookie-a", "item-100", "测试商品"
    )
    _put_config(
        {
            "mode": "fixed_link",
            "config": {"url": "https://private.example/s/secret?pwd=abc"},
        },
        card_id=binding["card_id"],
    )

    summary = reply_server._get_guided_delivery_summary("cookie-a")
    assert set(summary) == {
        "item_count",
        "delivery_configured",
        "republish_configured",
        "target_item_id",
        "target_item_title",
    }
    assert summary["delivery_configured"] is True
    assert summary["target_item_id"] == "item-100"
    assert "private.example" not in json.dumps(summary, ensure_ascii=False)


def test_setup_delivery_summary_ignores_orphan_and_damaged_configs(api_state, monkeypatch):
    class EmptyStore:
        def list_templates(self, cookie_id):
            return []

    monkeypatch.setattr(reply_server, "_get_republish_store", lambda: EmptyStore())
    monkeypatch.setattr(
        api_state,
        "get_cookie_details",
        lambda cookie_id: {"user_id": 1} if cookie_id == "cookie-a" else None,
    )

    _put_config({"mode": "fixed_link", "config": {"url": "https://orphan.example/secret"}})
    orphan_summary = reply_server._get_guided_delivery_summary("cookie-a")
    assert orphan_summary["delivery_configured"] is False

    binding = api_state.get_or_create_item_delivery_card(
        USER["user_id"], "cookie-a", "item-100", "测试商品"
    )
    _put_config(
        {"mode": "fixed_link", "config": {"url": "https://damaged.example/secret"}},
        card_id=binding["card_id"],
    )
    with api_state.lock:
        api_state.conn.execute(
            "UPDATE item_delivery_configs SET config_text = ? WHERE user_id = ? AND card_id = ? AND account_id = ?",
            ("broken-ciphertext", USER["user_id"], binding["card_id"], "cookie-a"),
        )
        api_state.conn.commit()

    damaged_summary = reply_server._get_guided_delivery_summary("cookie-a")
    assert damaged_summary["delivery_configured"] is False
    assert "damaged.example" not in json.dumps(damaged_summary, ensure_ascii=False)
