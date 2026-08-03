from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

import reply_server
from db_manager import DBManager


OWNER = {"user_id": 1, "username": "owner"}
OTHER_USER = {"user_id": 2, "username": "other"}


@pytest.fixture()
def binding_state(tmp_path, monkeypatch):
    manager = DBManager(str(tmp_path / "item-delivery-binding.sqlite3"))
    with manager.lock:
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
