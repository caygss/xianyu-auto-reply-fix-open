from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import db_manager as db_manager_module
import reply_server
from db_manager import DBManager, ITEM_DELIVERY_BINDING_MARKER


OWNER = {"user_id": 1, "username": "owner"}
OTHER_USER = {"user_id": 2, "username": "other"}


@pytest.fixture()
def binding_state(tmp_path, monkeypatch):
    manager = DBManager(str(tmp_path / "item-delivery-binding.sqlite3"))
    with manager.lock:
        manager.conn.execute(
            """
            INSERT OR IGNORE INTO users (id, username, email, password_hash)
            VALUES (2, 'other', 'other@example.test', 'test')
            """
        )
        manager.conn.execute(
            "INSERT OR IGNORE INTO cookies (id, value, user_id) VALUES (?, ?, ?)",
            ("account-a", "cookie-a", 1),
        )
        manager.conn.execute(
            "INSERT OR IGNORE INTO cookies (id, value, user_id) VALUES (?, ?, ?)",
            ("account-b", "cookie-b", 2),
        )
        manager.conn.execute(
            "INSERT OR IGNORE INTO item_info (cookie_id, item_id, item_title) VALUES (?, ?, ?)",
            ("account-a", "item-100", "测试商品"),
        )
        manager.conn.execute(
            "INSERT OR IGNORE INTO item_info (cookie_id, item_id, item_title) VALUES (?, ?, ?)",
            ("account-b", "item-200", "其他账号商品"),
        )
        manager.conn.commit()

    monkeypatch.setattr(reply_server, "db_manager", manager)
    monkeypatch.setattr(db_manager_module, "db_manager", manager)
    monkeypatch.setattr(
        manager,
        "get_all_cookies",
        lambda user_id=None: {
            1: {"account-a": "masked-cookie"},
            2: {"account-b": "masked-cookie"},
        }.get(user_id, {}),
    )
    yield manager
    manager.close()


def _call_endpoint(*, item_id="item-100", account_id="account-a", user=OWNER):
    return reply_server.create_item_delivery_card(
        item_id=item_id,
        account_id=account_id,
        current_user=user,
    )


def test_migration_creates_binding_table_primary_key_and_card_index(binding_state):
    columns = {
        row[1]: row
        for row in binding_state.conn.execute("PRAGMA table_info(item_delivery_bindings)")
    }
    assert set(columns) >= {
        "user_id",
        "account_id",
        "item_id",
        "card_id",
        "created_at",
        "updated_at",
    }
    assert [name for name, row in columns.items() if row[5]] == [
        "user_id",
        "account_id",
        "item_id",
    ]

    indexes = binding_state.conn.execute(
        "PRAGMA index_list(item_delivery_bindings)"
    ).fetchall()
    assert any(
        [column[2] for column in binding_state.conn.execute(f"PRAGMA index_info({row[1]})")]
        == ["card_id"]
        for row in indexes
    )


def test_database_connections_enable_foreign_keys_on_init_and_reconnect(binding_state):
    assert binding_state.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    binding_state.close()
    reconnected = binding_state.get_connection()

    assert reconnected.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_get_or_create_replaces_orphan_binding_with_one_real_card(binding_state):
    with binding_state.lock:
        binding_state.conn.commit()
        binding_state.conn.execute("PRAGMA foreign_keys = OFF")
        binding_state.conn.execute(
            """
            INSERT INTO item_delivery_bindings (user_id, account_id, item_id, card_id)
            VALUES (?, ?, ?, ?)
            """,
            (OWNER["user_id"], "account-a", "item-100", 999999),
        )
        binding_state.conn.commit()
        binding_state.conn.execute("PRAGMA foreign_keys = ON")

    first = binding_state.get_or_create_item_delivery_card(
        OWNER["user_id"], "account-a", "item-100", "测试商品"
    )
    second = binding_state.get_or_create_item_delivery_card(
        OWNER["user_id"], "account-a", "item-100", "测试商品"
    )

    assert first["card_id"] != 999999
    assert binding_state.get_card_by_id(first["card_id"], OWNER["user_id"])
    assert second == {"card_id": first["card_id"], "created": False}
    assert binding_state.conn.execute(
        "SELECT COUNT(*) FROM cards WHERE user_id = ?",
        (OWNER["user_id"],),
    ).fetchone()[0] == 1
    assert binding_state.conn.execute(
        """
        SELECT COUNT(*)
        FROM item_delivery_bindings b
        INNER JOIN cards c ON c.id = b.card_id AND c.user_id = b.user_id
        WHERE b.user_id = ? AND b.account_id = ? AND b.item_id = ?
        """,
        (OWNER["user_id"], "account-a", "item-100"),
    ).fetchone()[0] == 1


def test_binding_queries_require_existing_same_user_marker_card(binding_state):
    get_binding = getattr(binding_state, "get_item_delivery_binding", None)
    get_for_card = getattr(binding_state, "get_item_delivery_binding_for_card", None)
    assert callable(get_binding)
    assert callable(get_for_card)

    internal = binding_state.get_or_create_item_delivery_card(
        OWNER["user_id"], "account-a", "item-100", "测试商品"
    )
    binding = get_binding(OWNER["user_id"], "account-a", "item-100")
    assert binding == {
        "user_id": OWNER["user_id"],
        "account_id": "account-a",
        "item_id": "item-100",
        "card_id": internal["card_id"],
    }
    assert get_for_card(internal["card_id"]) == binding
    assert (
        get_for_card(
            internal["card_id"],
            user_id=OWNER["user_id"],
            account_id="account-a",
        )
        == binding
    )
    assert (
        get_for_card(
            internal["card_id"],
            user_id=OWNER["user_id"],
            account_id="account-b",
        )
        is None
    )
    assert get_binding(OTHER_USER["user_id"], "account-a", "item-100") is None

    ordinary_card_id = binding_state.create_card(
        name="普通卡券",
        card_type="text",
        description=f"用户备注含有 {ITEM_DELIVERY_BINDING_MARKER}",
        user_id=OWNER["user_id"],
    )
    assert get_for_card(ordinary_card_id) is None


def test_get_or_create_replaces_cross_user_or_non_marker_binding(binding_state):
    cross_user_card_id = binding_state.create_card(
        name="其他用户内部卡券",
        card_type="text",
        description=f"{ITEM_DELIVERY_BINDING_MARKER} legacy",
        user_id=OTHER_USER["user_id"],
    )
    with binding_state.lock:
        binding_state.conn.execute(
            """
            INSERT INTO item_delivery_bindings (user_id, account_id, item_id, card_id)
            VALUES (?, ?, ?, ?)
            """,
            (OWNER["user_id"], "account-a", "item-100", cross_user_card_id),
        )
        binding_state.conn.commit()

    resolved = binding_state.get_or_create_item_delivery_card(
        OWNER["user_id"], "account-a", "item-100", "测试商品"
    )

    assert resolved["card_id"] != cross_user_card_id
    owner_card = binding_state.get_card_by_id(resolved["card_id"], OWNER["user_id"])
    assert owner_card["description"].startswith(ITEM_DELIVERY_BINDING_MARKER)


def test_first_create_and_repeat_return_one_user_isolated_internal_card(binding_state):
    first = _call_endpoint()
    second = _call_endpoint()

    assert first == {
        "card_id": first["card_id"],
        "item_id": "item-100",
        "account_id": "account-a",
        "created": True,
    }
    assert second == {
        "card_id": first["card_id"],
        "item_id": "item-100",
        "account_id": "account-a",
        "created": False,
    }

    cards = binding_state.conn.execute(
        "SELECT id, name, type, description, user_id, api_config, text_content, data_content "
        "FROM cards WHERE user_id = ?",
        (OWNER["user_id"],),
    ).fetchall()
    assert len(cards) == 1
    card = cards[0]
    assert card[0] == first["card_id"]
    assert card[1] == "商品交付：测试商品"
    assert card[2] == "text"
    assert "item-delivery-binding" in card[3]
    assert card[4] == OWNER["user_id"]
    assert card[5:] == (None, None, None)


def test_internal_delivery_card_is_hidden_from_lists_but_available_by_id(binding_state):
    visible_card_id = binding_state.create_card(
        name="普通卡券",
        card_type="text",
        description="用户备注恰好包含 item-delivery-binding 文字",
        user_id=OWNER["user_id"],
    )
    binding = _call_endpoint()

    listed_ids = {
        card["id"] for card in binding_state.get_all_cards(OWNER["user_id"])
    }

    assert visible_card_id in listed_ids
    assert binding["card_id"] not in listed_ids
    assert binding_state.get_card_by_id(
        binding["card_id"], OWNER["user_id"]
    )["id"] == binding["card_id"]


def test_concurrent_get_or_create_produces_one_binding_and_one_card(binding_state):
    def create_once():
        return binding_state.get_or_create_item_delivery_card(
            OWNER["user_id"], "account-a", "item-100", "测试商品"
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: create_once(), range(16)))

    assert len({result["card_id"] for result in results}) == 1
    assert sum(result["created"] for result in results) == 1
    assert binding_state.conn.execute(
        "SELECT COUNT(*) FROM item_delivery_bindings"
    ).fetchone()[0] == 1
    assert binding_state.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("item_id", "account_id", "user", "status_code"),
    [
        ("item-100", "account-b", OWNER, 403),
        ("item-100", "account-a", OTHER_USER, 403),
        ("missing", "account-a", OWNER, 404),
        ("", "account-a", OWNER, 400),
        ("item-100", "", OWNER, 400),
    ],
)
def test_endpoint_rejects_cross_scope_missing_item_and_invalid_input(
    binding_state, item_id, account_id, user, status_code
):
    with pytest.raises(HTTPException) as error:
        _call_endpoint(item_id=item_id, account_id=account_id, user=user)
    assert error.value.status_code == status_code


def test_post_resolver_route_is_registered_and_authenticated():
    route = next(
        route
        for route in reply_server.app.routes
        if getattr(route, "path", "") == "/api/items/{item_id}/delivery-card"
    )
    assert route.methods == {"POST"}
    assert any(
        dependency.call is reply_server.get_current_user
        for dependency in route.dependant.dependencies
    )


def test_legacy_card_endpoints_reject_internal_cards_with_chinese_message(binding_state):
    internal = _call_endpoint()

    for operation in (
        lambda: reply_server.update_card(
            internal["card_id"], {"name": "篡改"}, OWNER
        ),
        lambda: reply_server.delete_card(internal["card_id"], OWNER),
    ):
        with pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 409
        assert "商品交付" in str(error.value.detail)

    assert binding_state.get_card_by_id(internal["card_id"], OWNER["user_id"])


def test_legacy_card_endpoints_still_update_and_delete_ordinary_cards(binding_state):
    update_id = binding_state.create_card(
        name="待更新", card_type="text", user_id=OWNER["user_id"]
    )
    delete_id = binding_state.create_card(
        name="待删除", card_type="text", user_id=OWNER["user_id"]
    )

    assert reply_server.update_card(update_id, {"name": "已更新"}, OWNER) == {
        "message": "卡券更新成功"
    }
    assert binding_state.get_card_by_id(update_id, OWNER["user_id"])["name"] == "已更新"
    assert reply_server.delete_card(delete_id, OWNER) == {"message": "卡券删除成功"}
    assert binding_state.get_card_by_id(delete_id, OWNER["user_id"]) is None


def test_legacy_card_http_routes_preserve_internal_binding_and_ordinary_behavior(
    binding_state, monkeypatch,
):
    internal = _call_endpoint()
    ordinary_id = binding_state.create_card(
        name="普通卡券", card_type="text", user_id=OWNER["user_id"]
    )
    previous = reply_server.app.dependency_overrides.get(reply_server.get_current_user)
    reply_server.app.dependency_overrides[reply_server.get_current_user] = lambda: OWNER
    monkeypatch.setattr(
        reply_server.image_manager,
        "save_image",
        lambda image_data, filename: "/static/uploads/images/test.png",
    )
    client = TestClient(reply_server.app)
    try:
        update_internal = client.put(
            f"/cards/{internal['card_id']}", json={"name": "篡改"}
        )
        image_update_internal = client.put(
            f"/cards/{internal['card_id']}/image",
            data={"name": "篡改", "type": "image", "description": ""},
            files={"image": ("test.png", b"not-written", "image/png")},
        )
        delete_internal = client.delete(f"/cards/{internal['card_id']}")
        update_ordinary = client.put(
            f"/cards/{ordinary_id}", json={"name": "已更新"}
        )
        delete_ordinary = client.delete(f"/cards/{ordinary_id}")
    finally:
        client.close()
        if previous is None:
            reply_server.app.dependency_overrides.pop(
                reply_server.get_current_user, None
            )
        else:
            reply_server.app.dependency_overrides[reply_server.get_current_user] = previous

    assert update_internal.status_code == 409
    assert image_update_internal.status_code == 409
    assert delete_internal.status_code == 409
    assert "商品交付" in update_internal.json()["detail"]
    assert "商品交付" in image_update_internal.json()["detail"]
    assert "商品交付" in delete_internal.json()["detail"]
    assert update_ordinary.status_code == 200
    assert delete_ordinary.status_code == 200
    assert binding_state.get_card_by_id(internal["card_id"], OWNER["user_id"])
    assert binding_state.get_item_delivery_binding(
        OWNER["user_id"], "account-a", "item-100"
    )["card_id"] == internal["card_id"]
    assert binding_state.get_card_by_id(ordinary_id, OWNER["user_id"]) is None
