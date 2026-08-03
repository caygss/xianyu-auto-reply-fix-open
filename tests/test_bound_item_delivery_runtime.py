import asyncio
import json
from collections import defaultdict

import pytest

import XianyuAutoAsync as xianyu_module
import db_manager as db_manager_module
import delivery_adapter_service
from card_inventory_service import CardInventoryService
from db_manager import DBManager
from delivery_adapter_service import ProviderResponse
from delivery_config_service import DeliveryConfigService
from delivery_orchestration_service import build_idempotency_key


USER_ID = 1
ACCOUNT_ID = "account-a"
ITEM_ID = "item-bound"


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    manager = DBManager(str(tmp_path / "bound-delivery-runtime.sqlite3"))
    with manager.lock:
        manager.conn.execute(
            """
            INSERT OR IGNORE INTO users (id, username, email, password_hash)
            VALUES (?, ?, ?, ?)
            """,
            (USER_ID, "runtime-owner", "runtime@example.test", "test"),
        )
        manager.conn.execute(
            "INSERT OR IGNORE INTO cookies (id, value, user_id) VALUES (?, ?, ?)",
            (ACCOUNT_ID, "cookie-value", USER_ID),
        )
        manager.conn.execute(
            """
            INSERT OR REPLACE INTO item_info (
                cookie_id, item_id, item_title, item_detail
            ) VALUES (?, ?, ?, ?)
            """,
            (ACCOUNT_ID, ITEM_ID, "绑定商品", "绑定商品详情"),
        )
        manager.conn.commit()

    monkeypatch.setattr(db_manager_module, "db_manager", manager)
    monkeypatch.setattr(xianyu_module, "db_manager", manager)

    live = xianyu_module.XianyuLive.__new__(xianyu_module.XianyuLive)
    live.user_id = USER_ID
    live.cookie_id = ACCOUNT_ID
    live.myid = "buyer-1"

    async def fetch_order_detail_info(*args, **kwargs):
        return None

    monkeypatch.setattr(live, "fetch_order_detail_info", fetch_order_detail_info)
    yield live, manager
    manager.close()


def create_binding(manager):
    return manager.get_or_create_item_delivery_card(
        USER_ID,
        ACCOUNT_ID,
        ITEM_ID,
        "绑定商品",
    )["card_id"]


class RecordingTransport:
    def __init__(self, content="provider-content"):
        self.content = content
        self.calls = []

    def request(self, method, url, headers, json_body, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json_body": dict(json_body),
                "timeout": timeout,
            }
        )
        return ProviderResponse(
            200,
            {"Content-Type": "application/json"},
            ('{"content":"%s"}' % self.content).encode("utf-8"),
        )


def test_bound_fixed_link_without_legacy_rule_still_prepares_link(runtime):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "fixed_link",
        {"url": "https://example.test/delivery"},
    )

    result = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            "order-fixed",
            "buyer-1",
            include_meta=True,
        )
    )

    assert result["success"] is True
    assert result["content"] == "https://example.test/delivery"
    assert result["configured"] is True
    assert result["card_id"] == card_id
    assert result["mode"] == "fixed_link"
    assert result["quantity"] == 1
    assert result["order_line_id"] == ITEM_ID
    assert result["idempotency_key"] == build_idempotency_key(
        USER_ID,
        ACCOUNT_ID,
        "order-fixed",
        ITEM_ID,
        card_id,
    )


def test_unbound_item_checks_binding_then_falls_back_to_legacy_rule(runtime, monkeypatch):
    live, manager = runtime
    binding_calls = []
    real_get_binding = manager.get_item_delivery_binding

    def get_binding(user_id, account_id, item_id):
        binding_calls.append((user_id, account_id, item_id))
        return real_get_binding(user_id, account_id, item_id)

    monkeypatch.setattr(manager, "get_item_delivery_binding", get_binding)
    monkeypatch.setattr(
        manager,
        "get_delivery_rules_by_keyword",
        lambda *args, **kwargs: [
            {
                "id": 11,
                "keyword": "绑定商品",
                "card_name": "旧规则卡券",
                "card_type": "text",
                "card_id": 22,
                "text_content": "legacy-content",
                "card_description": "",
                "spec_name": "",
                "spec_value": "",
                "spec_name_2": "",
                "spec_value_2": "",
            }
        ],
    )

    result = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            "order-legacy",
            "buyer-1",
            include_meta=True,
        )
    )

    assert binding_calls == [(USER_ID, ACCOUNT_ID, ITEM_ID)]
    assert result["success"] is True
    assert result["content"] == "legacy-content"
    assert result["rule_id"] == 11
    assert result.get("configured") is not True


def test_bound_card_shortage_pauses_whole_order_without_partial_reservation(runtime):
    live, manager = runtime
    card_id = create_binding(manager)
    configs = DeliveryConfigService(manager)
    inventory = CardInventoryService(manager)
    configs.save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "imported_card",
        {"source": "runtime-test"},
    )
    inventory.save_settings(card_id, USER_ID, ACCOUNT_ID, stock_ceiling=3)
    inventory.import_items(
        card_id,
        USER_ID,
        ACCOUNT_ID,
        ["card-secret-a", "card-secret-b"],
    )

    result = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            "order-shortage",
            "buyer-1",
            include_meta=True,
            quantity=3,
            order_line_id=ITEM_ID,
        )
    )

    assert result["success"] is False
    assert result["status"] == "paused"
    assert result["error_code"] == "insufficient_inventory"
    assert result["content"] is None
    assert result["delivery_steps"] == []
    assert result["reservation_id"] is None
    assert result["quantity"] == 3
    summary = inventory.get_inventory_summary(card_id, USER_ID, ACCOUNT_ID)
    assert summary["available"] == 2
    assert summary["reserved"] == 0


def test_bound_item_missing_config_never_queries_or_falls_back_to_legacy_rule(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    card_id = create_binding(manager)

    def fail_if_legacy_queried(*args, **kwargs):
        raise AssertionError("绑定商品配置缺失时不得查询旧发货规则")

    monkeypatch.setattr(
        manager,
        "get_delivery_rules_by_keyword",
        fail_if_legacy_queried,
    )

    result = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            "order-missing-config",
            "buyer-1",
            include_meta=True,
        )
    )

    assert result["success"] is False
    assert result["configured"] is True
    assert result["card_id"] == card_id
    assert result["status"] == "failed"
    assert result["error_code"] == "config_not_found"
    assert "交付配置不存在" in result["error"]
    assert result["content"] is None
    assert result["delivery_steps"] == []


@pytest.mark.parametrize("mode", ["imported_card", "generated_card"])
def test_bound_card_quantity_three_prepares_one_batch_with_three_distinct_cards(
    runtime,
    mode,
):
    live, manager = runtime
    card_id = create_binding(manager)
    configs = DeliveryConfigService(manager)
    inventory = CardInventoryService(manager)
    configs.save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        mode,
        {"source": mode},
    )
    inventory.save_settings(card_id, USER_ID, ACCOUNT_ID, stock_ceiling=3)
    if mode == "generated_card":
        inventory.generate_items(card_id, USER_ID, ACCOUNT_ID)
    else:
        inventory.import_items(
            card_id,
            USER_ID,
            ACCOUNT_ID,
            ["card-secret-a", "card-secret-b", "card-secret-c"],
        )

    result = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            "order-three-cards",
            "buyer-1",
            include_meta=True,
            quantity=3,
            order_line_id="line-three-cards",
        )
    )

    assert result["success"] is True
    assert result["status"] == "sending"
    assert result["configured"] is True
    assert result["mode"] == mode
    assert result["quantity"] == 3
    assert result["order_id"] == "order-three-cards"
    assert result["order_line_id"] == "line-three-cards"
    assert result["reservation_id"]
    assert result["claim_token"]
    assert result["content_count"] == 3
    assert len(result["contents"]) == 3
    assert len(set(result["contents"])) == 3
    assert [step["content"] for step in result["delivery_steps"]] == result["contents"]
    assert result["content"].splitlines() == result["contents"]

    with manager.lock:
        state_count = manager.conn.execute(
            "SELECT COUNT(*) FROM delivery_orchestration_states"
        ).fetchone()[0]
        reservation_count = manager.conn.execute(
            "SELECT COUNT(*) FROM card_inventory_reservations"
        ).fetchone()[0]
    assert state_count == 1
    assert reservation_count == 1
    summary = inventory.get_inventory_summary(card_id, USER_ID, ACCOUNT_ID)
    assert summary["available"] == 0
    assert summary["reserved"] == 0
    assert summary["sent"] == 3


def test_bound_provider_uses_internal_quantity_and_canonical_idempotency(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "provider_api",
        {
            "endpoint": "https://provider.test/issue",
            "request_body": {
                "quantity": 999,
                "idempotency_key": "forged-template-key",
            },
        },
    )
    transport = RecordingTransport()
    monkeypatch.setattr(
        delivery_adapter_service,
        "UrllibJsonTransport",
        lambda: transport,
    )

    result = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            "order-provider",
            "buyer-1",
            include_meta=True,
            quantity=3,
            order_line_id="line-provider",
        )
    )

    expected_key = build_idempotency_key(
        USER_ID,
        ACCOUNT_ID,
        "order-provider",
        "line-provider",
        card_id,
    )
    assert len(transport.calls) == 1
    assert transport.calls[0]["json_body"]["quantity"] == 3
    assert transport.calls[0]["json_body"]["idempotency_key"] == expected_key
    assert result["success"] is True
    assert result["mode"] == "provider_api"
    assert result["quantity"] == 3
    assert result["idempotency_key"] == expected_key
    assert result["contents"] == ["provider-content"]
    assert result["content_count"] == 1


def test_repeated_bound_prepare_does_not_reserve_again_or_return_new_claim(runtime):
    live, manager = runtime
    card_id = create_binding(manager)
    configs = DeliveryConfigService(manager)
    inventory = CardInventoryService(manager)
    configs.save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "imported_card",
        {"source": "repeat-test"},
    )
    inventory.save_settings(card_id, USER_ID, ACCOUNT_ID, stock_ceiling=4)
    inventory.import_items(
        card_id,
        USER_ID,
        ACCOUNT_ID,
        ["repeat-a", "repeat-b", "unused-c", "unused-d"],
    )
    call = dict(
        item_id=ITEM_ID,
        item_title="绑定商品",
        order_id="order-repeat",
        send_user_id="buyer-1",
        include_meta=True,
        quantity=2,
        order_line_id="line-repeat",
    )

    first = asyncio.run(live._auto_delivery(**call))
    repeated = asyncio.run(live._auto_delivery(**call))

    assert first["success"] is True
    assert first["status"] == "sending"
    assert first["claim_token"]
    assert len(first["contents"]) == 2
    assert repeated["success"] is False
    assert repeated["status"] == "in_progress"
    assert repeated["claim_token"] is None
    assert repeated["contents"] == []
    assert repeated["delivery_steps"] == []
    assert "处理中" in repeated["error"]

    with manager.lock:
        state_count = manager.conn.execute(
            "SELECT COUNT(*) FROM delivery_orchestration_states"
        ).fetchone()[0]
        reservation_count = manager.conn.execute(
            "SELECT COUNT(*) FROM card_inventory_reservations"
        ).fetchone()[0]
    assert state_count == 1
    assert reservation_count == 1
    summary = inventory.get_inventory_summary(card_id, USER_ID, ACCOUNT_ID)
    assert summary["available"] == 2
    assert summary["sent"] == 2


def test_multi_quantity_call_plan_prepares_bound_once_and_legacy_per_unit(runtime):
    live, manager = runtime
    card_id = create_binding(manager)

    bound_plan = live._build_auto_delivery_preparation_plan(ITEM_ID, 3)

    assert bound_plan == [
        {
            "unit_index": 1,
            "quantity": 3,
            "order_line_id": ITEM_ID,
            "configured": True,
            "card_id": card_id,
        }
    ]

    with manager.lock:
        manager.conn.execute(
            "DELETE FROM item_delivery_bindings WHERE item_id = ?",
            (ITEM_ID,),
        )
        manager.conn.commit()

    legacy_plan = live._build_auto_delivery_preparation_plan(ITEM_ID, 3)

    assert legacy_plan == [
        {
            "unit_index": unit_index,
            "quantity": 1,
            "order_line_id": None,
            "configured": False,
            "card_id": None,
        }
        for unit_index in range(1, 4)
    ]


def test_bound_invalid_config_fails_closed_without_legacy_lookup(runtime, monkeypatch):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "fixed_link",
        {"url": "https://example.test/valid"},
    )
    invalid_config = manager._encrypt_secret(
        json.dumps({"url": "not-a-valid-link"}, ensure_ascii=False)
    )
    with manager.lock:
        manager.conn.execute(
            """
            UPDATE item_delivery_configs
            SET config_text = ?
            WHERE user_id = ? AND card_id = ? AND account_id = ?
            """,
            (invalid_config, USER_ID, card_id, ACCOUNT_ID),
        )
        manager.conn.commit()

    monkeypatch.setattr(
        manager,
        "get_delivery_rules_by_keyword",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("绑定商品配置错误时不得查询旧发货规则")
        ),
    )

    result = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            "order-invalid-config",
            "buyer-1",
            include_meta=True,
        )
    )

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["error_code"] == "invalid_config"
    assert "固定链接" in result["error"]
    assert result["content"] is None
    assert result["delivery_steps"] == []


def test_bound_provider_failed_state_returns_chinese_blocking_result(runtime, monkeypatch):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "provider_api",
        {"endpoint": "https://provider.test/issue"},
    )

    class MissingContentTransport:
        def request(self, method, url, headers, json_body, timeout):
            return ProviderResponse(200, {}, b"{}")

    monkeypatch.setattr(
        delivery_adapter_service,
        "UrllibJsonTransport",
        MissingContentTransport,
    )

    result = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            "order-provider-failed",
            "buyer-1",
            include_meta=True,
            order_line_id="line-provider-failed",
        )
    )

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["error_code"] == "provider_response_field_missing"
    assert "交付内容" in result["error"]
    assert result["claim_token"] is None
    assert result["contents"] == []
    with manager.lock:
        state = manager.conn.execute(
            "SELECT status, claim_token FROM delivery_orchestration_states"
        ).fetchone()
    assert tuple(state) == ("failed", None)


def test_bound_sent_state_blocks_duplicate_content_and_claim(runtime):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "fixed_link",
        {"url": "https://example.test/already-sent"},
    )
    call = dict(
        item_id=ITEM_ID,
        item_title="绑定商品",
        order_id="order-sent",
        send_user_id="buyer-1",
        include_meta=True,
        order_line_id="line-sent",
    )
    first = asyncio.run(live._auto_delivery(**call))
    assert first["success"] is True

    with manager.lock:
        manager.conn.execute(
            """
            UPDATE delivery_orchestration_states
            SET status = 'sent', claim_token = NULL, claimed_at = NULL
            WHERE order_id = ? AND order_line_id = ?
            """,
            ("order-sent", "line-sent"),
        )
        manager.conn.commit()

    repeated = asyncio.run(live._auto_delivery(**call))

    assert repeated["success"] is False
    assert repeated["status"] == "sent"
    assert "已经发货完成" in repeated["error"]
    assert repeated["claim_token"] is None
    assert repeated["content"] is None
    assert repeated["contents"] == []
    assert repeated["delivery_steps"] == []


def test_configured_result_metadata_is_preserved_without_config_secret_or_log_token(runtime):
    live, _ = runtime
    delivery_result = {
        "success": True,
        "configured": True,
        "card_id": 7,
        "card_type": "data",
        "mode": "imported_card",
        "quantity": 3,
        "order_id": "order-meta",
        "order_line_id": "line-meta",
        "idempotency_key": "canonical-key",
        "reservation_id": "reservation-meta",
        "claim_token": "internal-claim-token",
        "orchestration_status": "sending",
        "content_count": 3,
        "orchestration_meta": {"content_count": 3, "state": None},
        "match_mode": "item_delivery_binding",
        "order_spec_mode": "bound_item",
        "item_config_mode": "bound_item",
        "delivery_unit_index": 1,
        "config": {"token": "must-not-be-saved"},
        "contents": ["secret-a", "secret-b", "secret-c"],
    }

    meta = live._delivery_result_to_rule_meta(delivery_result)

    assert meta["configured"] is True
    assert meta["card_id"] == 7
    assert meta["mode"] == "imported_card"
    assert meta["quantity"] == 3
    assert meta["order_id"] == "order-meta"
    assert meta["order_line_id"] == "line-meta"
    assert meta["idempotency_key"] == "canonical-key"
    assert meta["reservation_id"] == "reservation-meta"
    assert meta["claim_token"] == "internal-claim-token"
    assert meta["orchestration_status"] == "sending"
    assert meta["content_count"] == 3
    assert meta["orchestration_meta"] == {"content_count": 3, "state": None}
    assert "config" not in meta
    assert "contents" not in meta
    visible_reason = live._format_delivery_log_reason("发货准备完成", meta)
    assert "internal-claim-token" not in visible_reason


def test_main_preparation_reads_bound_quantity_when_legacy_toggle_is_false(runtime, monkeypatch):
    live, manager = runtime
    card_id = create_binding(manager)
    fetch_calls = []

    async def fetch_quantity_three(*args, **kwargs):
        fetch_calls.append((args, kwargs))
        return {"quantity": "3"}

    monkeypatch.setattr(live, "fetch_order_detail_info", fetch_quantity_three)
    monkeypatch.setattr(
        manager,
        "get_item_multi_quantity_delivery_status",
        lambda *args, **kwargs: False,
    )

    quantity, plan = asyncio.run(
        live._build_order_auto_delivery_preparation_plan(
            ITEM_ID,
            "order-main-bound",
            "buyer-1",
        )
    )

    assert len(fetch_calls) == 1
    assert quantity == 3
    assert plan == [
        {
            "unit_index": 1,
            "quantity": 3,
            "order_line_id": ITEM_ID,
            "configured": True,
            "card_id": card_id,
        }
    ]

    async def fetch_invalid_quantity(*args, **kwargs):
        return {"quantity": "invalid"}

    monkeypatch.setattr(live, "fetch_order_detail_info", fetch_invalid_quantity)
    invalid_quantity, invalid_plan = asyncio.run(
        live._build_order_auto_delivery_preparation_plan(
            ITEM_ID,
            "order-main-invalid",
            "buyer-1",
        )
    )
    assert invalid_quantity == 1
    assert invalid_plan[0]["quantity"] == 1

    with manager.lock:
        manager.conn.execute(
            "DELETE FROM item_delivery_bindings WHERE item_id = ?",
            (ITEM_ID,),
        )
        manager.conn.commit()

    async def fail_if_legacy_fetches(*args, **kwargs):
        raise AssertionError("legacy 开关为 false 时不得读取订单多数量")

    monkeypatch.setattr(live, "fetch_order_detail_info", fail_if_legacy_fetches)
    quantity, plan = asyncio.run(
        live._build_order_auto_delivery_preparation_plan(
            ITEM_ID,
            "order-main-legacy",
            "buyer-1",
        )
    )

    assert quantity == 1
    assert len(plan) == 1
    assert plan[0]["configured"] is False
    assert plan[0]["quantity"] == 1


def test_manual_entry_prepares_bound_whole_order_once_and_legacy_remaining_units(
    runtime,
    monkeypatch,
):
    import cookie_manager
    import reply_server

    live, manager = runtime
    create_binding(manager)
    remaining = {"indexes": [1, 2, 3]}
    auto_delivery_calls = []
    prepared_batches = []
    order = {
        "order_id": "order-manual",
        "cookie_id": ACCOUNT_ID,
        "item_id": ITEM_ID,
        "buyer_id": "buyer-1",
        "buyer_nick": "买家",
        "quantity": "3",
        "sid": "",
    }

    monkeypatch.setattr(manager, "get_order_by_id", lambda order_id: dict(order))
    monkeypatch.setattr(
        manager,
        "get_cookie_details",
        lambda cookie_id: {"id": cookie_id, "user_id": USER_ID},
    )
    monkeypatch.setattr(
        manager,
        "get_item_info",
        lambda cookie_id, item_id: {"item_title": "绑定商品"},
    )
    monkeypatch.setattr(manager, "create_delivery_log", lambda **kwargs: True)
    monkeypatch.setattr(reply_server, "db_manager", manager)
    monkeypatch.setattr(
        reply_server.blacklist_service,
        "is_buyer_blacklisted",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(reply_server, "publish_order_update_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(reply_server, "log_with_user", lambda *args, **kwargs: None)

    class RunningManager:
        @staticmethod
        def get_xianyu_instance(cookie_id):
            return live

    monkeypatch.setattr(cookie_manager, "manager", RunningManager())
    live.ws = None
    monkeypatch.setattr(
        live,
        "_summarize_delivery_progress",
        lambda order_id, expected_quantity: {
            "pending_finalize_unit_indexes": [],
            "remaining_unit_indexes": list(remaining["indexes"]),
            "aggregate_status": "pending",
        },
    )
    monkeypatch.setattr(
        live,
        "_sync_order_delivery_progress",
        lambda **kwargs: {
            "aggregate_status": "pending",
            "finalized_count": 0,
            "remaining_count": len(remaining["indexes"]),
        },
    )

    def capture_prepared_units(prepared_units, total_units):
        prepared_batches.append((list(prepared_units), total_units))
        return []

    monkeypatch.setattr(live, "_build_delivery_send_groups", capture_prepared_units)

    async def record_auto_delivery(*args, **kwargs):
        auto_delivery_calls.append((args, kwargs))
        return {
            "success": True,
            "configured": kwargs.get("order_line_id") is not None,
            "content": "prepared-content",
            "delivery_steps": [{"type": "text", "content": "prepared-content"}],
            "card_id": 7,
            "card_type": "data",
            "mode": "imported_card",
            "quantity": kwargs.get("quantity"),
            "order_id": kwargs.get("order_id"),
            "order_line_id": kwargs.get("order_line_id"),
            "idempotency_key": "canonical-key",
            "reservation_id": "reservation-manual",
            "claim_token": "claim-manual",
            "orchestration_status": "sending",
            "content_count": kwargs.get("quantity"),
            "orchestration_meta": {"content_count": kwargs.get("quantity")},
        }

    monkeypatch.setattr(live, "_auto_delivery", record_auto_delivery)

    asyncio.run(
        reply_server.manual_deliver_order(
            "order-manual",
            current_user={"user_id": USER_ID, "username": "owner"},
        )
    )

    assert len(auto_delivery_calls) == 1
    assert auto_delivery_calls[0][1]["quantity"] == 3
    assert auto_delivery_calls[0][1]["order_line_id"] == ITEM_ID
    bound_rule_meta = prepared_batches[0][0][0]["rule_meta"]
    assert bound_rule_meta["configured"] is True
    assert bound_rule_meta["quantity"] == 3
    assert bound_rule_meta["order_line_id"] == ITEM_ID
    assert bound_rule_meta["claim_token"] == "claim-manual"
    assert bound_rule_meta["orchestration_status"] == "sending"

    with manager.lock:
        manager.conn.execute(
            "DELETE FROM item_delivery_bindings WHERE item_id = ?",
            (ITEM_ID,),
        )
        manager.conn.commit()
    remaining["indexes"] = [2, 3]
    auto_delivery_calls.clear()
    prepared_batches.clear()

    asyncio.run(
        reply_server.manual_deliver_order(
            "order-manual",
            current_user={"user_id": USER_ID, "username": "owner"},
        )
    )

    assert len(auto_delivery_calls) == 2
    assert [call[1]["delivery_unit_index"] for call in auto_delivery_calls] == [2, 3]


def test_recovered_entry_passes_bound_order_quantity_and_preserves_legacy_single_call(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    create_binding(manager)
    auto_delivery_calls = []
    live._order_locks = defaultdict(asyncio.Lock)
    live._lock_usage_times = {}
    monkeypatch.setattr(live, "can_auto_delivery", lambda order_id: True)
    monkeypatch.setattr(live, "is_lock_held", lambda lock_key: False)
    monkeypatch.setattr(live, "_get_pending_delivery_finalization_meta", lambda *args: None)
    monkeypatch.setattr(live, "_record_delivery_log", lambda **kwargs: None)

    async def record_auto_delivery(*args, **kwargs):
        auto_delivery_calls.append((args, kwargs))
        return {
            "success": False,
            "content": None,
            "delivery_steps": [],
            "error": "测试停止在准备阶段",
        }

    monkeypatch.setattr(live, "_auto_delivery", record_auto_delivery)
    order = {"quantity": "3"}

    asyncio.run(
        live._send_recovered_delivery_without_sid(
            order,
            order_id="order-recovered-bound",
            item_id=ITEM_ID,
            buyer_id="buyer-1",
            source="test-recovery",
        )
    )

    assert len(auto_delivery_calls) == 1
    assert auto_delivery_calls[0][1]["quantity"] == 3
    assert auto_delivery_calls[0][1]["order_line_id"] == ITEM_ID

    with manager.lock:
        manager.conn.execute(
            "DELETE FROM item_delivery_bindings WHERE item_id = ?",
            (ITEM_ID,),
        )
        manager.conn.commit()
    auto_delivery_calls.clear()

    asyncio.run(
        live._send_recovered_delivery_without_sid(
            order,
            order_id="order-recovered-legacy",
            item_id=ITEM_ID,
            buyer_id="buyer-1",
            source="test-recovery",
        )
    )

    assert len(auto_delivery_calls) == 1
