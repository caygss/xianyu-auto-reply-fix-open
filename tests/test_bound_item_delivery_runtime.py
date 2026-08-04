import asyncio
import json
import time
from collections import defaultdict

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

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
    assert result["_orchestration_private"]["claim_token"]
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
    assert first["_orchestration_private"]["claim_token"]
    assert len(first["contents"]) == 2
    assert repeated["success"] is False
    assert repeated["status"] == "in_progress"
    assert repeated["disposition"] == "defer_in_progress"
    assert repeated["_orchestration_private"] == {}
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
            "binding_snapshot": {
                "user_id": USER_ID,
                "account_id": ACCOUNT_ID,
                "item_id": ITEM_ID,
                "card_id": card_id,
            },
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
            "binding_snapshot": None,
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
    assert "交付配置" in result["error"]
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
    assert "发货准备失败" in result["error"]
    assert result["_orchestration_private"] == {}
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
    assert repeated["disposition"] == "noop_sent"
    assert "已经发货完成" in repeated["error"]
    assert repeated["_orchestration_private"] == {}
    assert repeated["content"] is None
    assert repeated["contents"] == []
    assert repeated["delivery_steps"] == []


def test_configured_result_metadata_is_preserved_without_config_secret_or_log_token(runtime):
    live, manager = runtime
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
    assert "claim_token" not in meta
    assert "_orchestration_private" not in meta
    assert meta["orchestration_status"] == "sending"
    assert meta["content_count"] == 3
    assert meta["orchestration_meta"] == {"content_count": 3, "state": None}
    assert "config" not in meta
    assert "contents" not in meta
    visible_reason = live._format_delivery_log_reason("发货准备完成", meta)
    assert "internal-claim-token" not in visible_reason
    live._record_delivery_log(
        order_id="order-meta",
        item_id=ITEM_ID,
        buyer_id="buyer-1",
        reason="发货准备完成",
        rule_meta=delivery_result,
    )
    with manager.lock:
        reason = manager.conn.execute(
            "SELECT reason FROM delivery_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert "internal-claim-token" not in reason


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
            "binding_snapshot": {
                "user_id": USER_ID,
                "account_id": ACCOUNT_ID,
                "item_id": ITEM_ID,
                "card_id": card_id,
            },
        }
    ]

    async def fetch_invalid_quantity(*args, **kwargs):
        return {"quantity": "invalid"}

    monkeypatch.setattr(live, "fetch_order_detail_info", fetch_invalid_quantity)
    monkeypatch.setattr(manager, "get_order_by_id", lambda order_id: None)
    invalid_quantity, invalid_plan = asyncio.run(
        live._build_order_auto_delivery_preparation_plan(
            ITEM_ID,
            "order-main-invalid",
            "buyer-1",
        )
    )
    assert invalid_quantity is None
    assert invalid_plan == [
        {
            "unit_index": 1,
            "quantity": None,
            "order_line_id": ITEM_ID,
            "configured": True,
            "card_id": card_id,
            "binding_snapshot": {
                "user_id": USER_ID,
                "account_id": ACCOUNT_ID,
                "item_id": ITEM_ID,
                "card_id": card_id,
            },
            "preparation_status": "blocked",
            "retryable": True,
            "error_code": "quantity_unavailable",
            "error": "暂时无法确认订单购买数量，请稍后重试",
        }
    ]
    with manager.lock:
        assert manager.conn.execute(
            "SELECT COUNT(*) FROM delivery_orchestration_states"
        ).fetchone()[0] == 0

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
            "_orchestration_private": {"claim_token": "claim-manual"},
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
    assert bound_rule_meta["_orchestration_private"]["claim_token"] == "claim-manual"
    assert bound_rule_meta["orchestration_status"] == "sending"

    remaining["indexes"] = [2, 3]
    auto_delivery_calls.clear()
    prepared_batches.clear()
    assert live._get_bound_item_delivery(ITEM_ID) is not None
    assert live._build_auto_delivery_preparation_plan(
        ITEM_ID,
        3,
        legacy_unit_indexes=[2, 3],
    )[0]["configured"] is True

    with pytest.raises(HTTPException) as partial_error:
        asyncio.run(
            reply_server.manual_deliver_order(
                "order-manual",
                current_user={"user_id": USER_ID, "username": "owner"},
            )
        )

    assert partial_error.value.status_code == 409
    assert "存在部分历史发货记录，请先核对" in str(partial_error.value.detail)
    assert auto_delivery_calls == []
    assert prepared_batches == []

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


def _configure_conflicting_manual_delivery(monkeypatch, live, manager, order_id):
    reply_server = _configure_manual_bound_route(monkeypatch, live, manager, order_id)
    conflict_summary = {
        "coverage_conflict": True,
        "conflict_unit_indexes": [1, 2],
        "pending_finalize_unit_indexes": [1],
        "remaining_unit_indexes": [],
        "aggregate_status": "pending_ship",
    }
    finalize_calls = []
    send_calls = []

    monkeypatch.setattr(
        live,
        "_summarize_delivery_progress",
        lambda value, expected_quantity: conflict_summary,
    )

    async def record_finalize(**kwargs):
        finalize_calls.append(kwargs)
        return {"success": True}

    async def record_send(*args, **kwargs):
        send_calls.append((args, kwargs))

    monkeypatch.setattr(live, "_finalize_delivery_after_send", record_finalize)
    monkeypatch.setattr(live, "send_delivery_steps_once", record_send)
    return reply_server, finalize_calls, send_calls


def test_manual_delivery_function_rejects_conflict_before_pending_finalize(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    create_binding(manager)
    reply_server, finalize_calls, send_calls = _configure_conflicting_manual_delivery(
        monkeypatch,
        live,
        manager,
        "order-manual-conflict-function",
    )

    with pytest.raises(HTTPException) as conflict_error:
        asyncio.run(
            reply_server.manual_deliver_order(
                "order-manual-conflict-function",
                current_user={"user_id": USER_ID, "username": "owner"},
            )
        )

    assert conflict_error.value.status_code == 409
    assert "发货记录冲突，请先核对" in str(conflict_error.value.detail)
    assert finalize_calls == []
    assert send_calls == []


def test_manual_delivery_http_rejects_conflict_before_pending_finalize(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    create_binding(manager)
    reply_server, finalize_calls, send_calls = _configure_conflicting_manual_delivery(
        monkeypatch,
        live,
        manager,
        "order-manual-conflict-http",
    )
    previous_override = reply_server.app.dependency_overrides.get(reply_server.get_current_user)
    reply_server.app.dependency_overrides[reply_server.get_current_user] = lambda: {
        "user_id": USER_ID,
        "username": "owner",
    }

    try:
        client = TestClient(reply_server.app)
        response = client.post("/api/orders/order-manual-conflict-http/deliver")
    finally:
        if 'client' in locals():
            client.close()
        if previous_override is None:
            reply_server.app.dependency_overrides.pop(reply_server.get_current_user, None)
        else:
            reply_server.app.dependency_overrides[reply_server.get_current_user] = previous_override

    assert response.status_code == 409
    assert "发货记录冲突，请先核对" in response.json()["detail"]
    assert finalize_calls == []
    assert send_calls == []


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


@pytest.mark.parametrize("mutation", ["deleted", "rebound", "added"])
def test_preparation_binding_snapshot_fails_closed_when_binding_changes(
    runtime,
    monkeypatch,
    mutation,
):
    live, manager = runtime
    if mutation == "added":
        plan = live._build_auto_delivery_preparation_plan(ITEM_ID, 1)
        create_binding(manager)
    else:
        original_card_id = create_binding(manager)
        plan = live._build_auto_delivery_preparation_plan(ITEM_ID, 1)
        with manager.lock:
            manager.conn.execute(
                "DELETE FROM item_delivery_bindings WHERE item_id = ?",
                (ITEM_ID,),
            )
            manager.conn.commit()
        if mutation == "rebound":
            replacement_card_id = create_binding(manager)
            assert replacement_card_id != original_card_id

    monkeypatch.setattr(
        manager,
        "get_delivery_rules_by_keyword",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("binding snapshot mismatch must not reach legacy delivery")
        ),
    )

    result = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            f"order-binding-{mutation}",
            "buyer-1",
            include_meta=True,
            quantity=plan[0]["quantity"],
            order_line_id=plan[0]["order_line_id"],
            binding_snapshot=plan[0]["binding_snapshot"],
        )
    )

    assert result["success"] is False
    assert result["disposition"] == "failed"
    assert result["error_code"] == "binding_changed"
    assert result["error"] == "商品交付绑定已变化，请稍后重试"
    with manager.lock:
        assert manager.conn.execute(
            "SELECT COUNT(*) FROM delivery_orchestration_states"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("mutation", ["deleted", "rebound"])
def test_bound_prepare_binding_change_before_service_insert_creates_no_state_or_reservation(
    runtime,
    monkeypatch,
    mutation,
):
    live, manager = runtime
    original_card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        original_card_id,
        ACCOUNT_ID,
        "imported_card",
        {"source": "atomic-binding-test"},
    )
    inventory = CardInventoryService(manager)
    inventory.save_settings(
        original_card_id,
        USER_ID,
        ACCOUNT_ID,
        stock_ceiling=1,
    )
    inventory.import_items(
        original_card_id,
        USER_ID,
        ACCOUNT_ID,
        ["must-not-be-reserved"],
    )
    plan = live._build_auto_delivery_preparation_plan(ITEM_ID, 1)
    real_prepare = live._prepare_bound_item_delivery
    replacement_card_ids = []

    def mutate_after_caller_precheck(**kwargs):
        with manager.lock:
            manager.conn.execute(
                "DELETE FROM item_delivery_bindings WHERE item_id = ?",
                (ITEM_ID,),
            )
            manager.conn.commit()
        if mutation == "rebound":
            replacement_card_ids.append(create_binding(manager))
        return real_prepare(**kwargs)

    monkeypatch.setattr(
        live,
        "_prepare_bound_item_delivery",
        mutate_after_caller_precheck,
    )

    result = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            f"order-atomic-binding-{mutation}",
            "buyer-1",
            include_meta=True,
            quantity=plan[0]["quantity"],
            order_line_id=plan[0]["order_line_id"],
            binding_snapshot=plan[0]["binding_snapshot"],
        )
    )

    if mutation == "rebound":
        assert replacement_card_ids and replacement_card_ids[0] != original_card_id
    else:
        assert replacement_card_ids == []
    assert result["success"] is False
    assert result["error_code"] == "binding_changed"
    with manager.lock:
        assert manager.conn.execute(
            "SELECT COUNT(*) FROM delivery_orchestration_states"
        ).fetchone()[0] == 0
        assert manager.conn.execute(
            "SELECT COUNT(*) FROM card_inventory_reservations"
        ).fetchone()[0] == 0


def test_bound_provider_prepare_does_not_block_event_loop(runtime, monkeypatch):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "provider_api",
        {"endpoint": "https://provider.test/slow"},
    )

    class SlowTransport(RecordingTransport):
        def request(self, method, url, headers, json_body, timeout):
            time.sleep(0.2)
            return super().request(method, url, headers, json_body, timeout)

    monkeypatch.setattr(
        delivery_adapter_service,
        "UrllibJsonTransport",
        SlowTransport,
    )

    async def exercise():
        started = asyncio.get_running_loop().time()
        delivery_task = asyncio.create_task(
            live._auto_delivery(
                ITEM_ID,
                "绑定商品",
                "order-slow-provider",
                "buyer-1",
                include_meta=True,
                quantity=1,
                order_line_id=ITEM_ID,
            )
        )
        await asyncio.sleep(0.02)
        ticker_latency = asyncio.get_running_loop().time() - started
        result = await delivery_task
        return ticker_latency, result

    ticker_latency, result = asyncio.run(exercise())

    assert ticker_latency < 0.1
    assert result["success"] is True


def test_bound_provider_exception_is_sanitized_in_result_state_and_logs(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    card_id = create_binding(manager)
    secrets = [
        "https://provider.test/private?token=url-secret",
        "header-secret-token",
        "response-secret-body",
        "secret-card-content",
    ]
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "provider_api",
        {
            "endpoint": secrets[0],
            "token": secrets[1],
            "request_body": {"card": secrets[3]},
        },
    )

    class SecretFailureTransport:
        def request(self, method, url, headers, json_body, timeout):
            raise RuntimeError(" ".join(secrets))

    logged = []
    monkeypatch.setattr(delivery_adapter_service, "UrllibJsonTransport", SecretFailureTransport)
    monkeypatch.setattr(xianyu_module.logger, "error", lambda message: logged.append(str(message)))

    result = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            "order-provider-secret",
            "buyer-1",
            include_meta=True,
            quantity=1,
            order_line_id=ITEM_ID,
        )
    )

    with manager.lock:
        stored_error = manager.conn.execute(
            "SELECT last_error FROM delivery_orchestration_states WHERE order_id = ?",
            ("order-provider-secret",),
        ).fetchone()[0]
    public_text = json.dumps(result, ensure_ascii=False)
    assert result["error_code"] == "provider_transport_error"
    assert result["error"] == "绑定商品发货准备失败，请稍后重试"
    for secret in secrets:
        assert secret not in public_text
        assert secret not in stored_error
        assert all(secret not in entry for entry in logged)


def _configure_manual_bound_route(monkeypatch, live, manager, order_id):
    import cookie_manager
    import reply_server

    order = {
        "order_id": order_id,
        "cookie_id": ACCOUNT_ID,
        "item_id": ITEM_ID,
        "buyer_id": "buyer-1",
        "buyer_nick": "买家",
        "quantity": "1",
        "sid": "",
    }
    monkeypatch.setattr(manager, "get_order_by_id", lambda value: dict(order))
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
        lambda value, expected_quantity: {
            "pending_finalize_unit_indexes": [],
            "remaining_unit_indexes": [1],
            "aggregate_status": "pending",
        },
    )
    monkeypatch.setattr(
        live,
        "_sync_order_delivery_progress",
        lambda **kwargs: {
            "aggregate_status": "shipped",
            "finalized_count": 1,
            "remaining_count": 0,
            "pending_finalize_count": 0,
        },
    )
    return reply_server


def test_manual_bound_route_uses_real_orchestration_and_keeps_claim_private(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "fixed_link",
        {"url": "https://example.test/manual-real"},
    )
    reply_server = _configure_manual_bound_route(
        monkeypatch,
        live,
        manager,
        "order-manual-real",
    )
    sent_steps = []

    async def record_send(buyer_id, item_id, steps):
        sent_steps.append(list(steps))

    async def finalize_without_network(**kwargs):
        return {"success": True}

    monkeypatch.setattr(live, "send_delivery_steps_once", record_send)
    monkeypatch.setattr(live, "_finalize_delivery_after_send", finalize_without_network)
    monkeypatch.setattr(live, "_mark_data_reservation_sent_if_needed", lambda meta: True)

    response = asyncio.run(
        reply_server.manual_deliver_order(
            "order-manual-real",
            current_user={"user_id": USER_ID, "username": "owner"},
        )
    )

    assert sent_steps == [[{"type": "text", "content": "https://example.test/manual-real"}]]
    with manager.lock:
        state_count = manager.conn.execute(
            "SELECT COUNT(*) FROM delivery_orchestration_states"
        ).fetchone()[0]
        claim_token = manager.conn.execute(
            "SELECT claim_token FROM delivery_orchestration_states"
        ).fetchone()[0]
        log_reasons = [
            row[0]
            for row in manager.conn.execute(
                "SELECT reason FROM delivery_logs WHERE order_id = ?",
                ("order-manual-real",),
            ).fetchall()
        ]
    finalization = manager.get_delivery_finalization_state("order-manual-real", 1)

    assert state_count == 1
    assert claim_token
    assert finalization["delivery_meta"]["_orchestration_private"]["claim_token"] == claim_token
    assert claim_token not in json.dumps(response, ensure_ascii=False)
    assert all(claim_token not in reason for reason in log_reasons)


@pytest.mark.parametrize(
    ("state_status", "expected_disposition"),
    [("sending", "defer_in_progress"), ("sent", "noop_sent")],
)
def test_manual_bound_sent_or_in_progress_is_skipped_without_send_or_failure(
    runtime,
    monkeypatch,
    state_status,
    expected_disposition,
):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "fixed_link",
        {"url": "https://example.test/manual-repeat"},
    )
    reply_server = _configure_manual_bound_route(
        monkeypatch,
        live,
        manager,
        f"order-manual-{state_status}",
    )
    first = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            f"order-manual-{state_status}",
            "buyer-1",
            include_meta=True,
            quantity=1,
            order_line_id=ITEM_ID,
        )
    )
    assert first["disposition"] == "send"
    if state_status == "sent":
        with manager.lock:
            manager.conn.execute(
                """
                UPDATE delivery_orchestration_states
                SET status = 'sent', claim_token = NULL, claimed_at = NULL
                WHERE order_id = ?
                """,
                (f"order-manual-{state_status}",),
            )
            manager.conn.commit()

    async def fail_if_sent(*args, **kwargs):
        raise AssertionError("sent/in_progress disposition must not send")

    monkeypatch.setattr(live, "send_delivery_steps_once", fail_if_sent)

    response = asyncio.run(
        reply_server.manual_deliver_order(
            f"order-manual-{state_status}",
            current_user={"user_id": USER_ID, "username": "owner"},
        )
    )

    with manager.lock:
        logs = manager.conn.execute(
            "SELECT status, reason FROM delivery_logs WHERE order_id = ?",
            (f"order-manual-{state_status}",),
        ).fetchall()
    assert response["success"] is True
    assert logs
    assert {row[0] for row in logs} == {"skipped"}
    assert expected_disposition in logs[-1][1]


@pytest.mark.parametrize(
    ("disposition", "expected_result"),
    [("defer_in_progress", False), ("noop_sent", True)],
)
def test_compensation_disposition_is_skipped_without_failure_log(
    runtime,
    monkeypatch,
    disposition,
    expected_result,
):
    live, manager = runtime
    create_binding(manager)
    live._order_locks = defaultdict(asyncio.Lock)
    live._lock_usage_times = {}
    monkeypatch.setattr(live, "can_auto_delivery", lambda order_id: True)
    monkeypatch.setattr(live, "is_lock_held", lambda lock_key: False)
    monkeypatch.setattr(live, "_get_pending_delivery_finalization_meta", lambda *args: None)
    logs = []
    monkeypatch.setattr(live, "_record_delivery_log", lambda **kwargs: logs.append(kwargs))

    async def disposition_result(*args, **kwargs):
        return {
            "success": False,
            "configured": True,
            "content": None,
            "delivery_steps": [],
            "disposition": disposition,
            "error": "重复准备已阻止",
        }

    async def fail_if_sent(*args, **kwargs):
        raise AssertionError("sent/in_progress compensation must not send")

    monkeypatch.setattr(live, "_auto_delivery", disposition_result)
    monkeypatch.setattr(live, "send_delivery_steps_once", fail_if_sent)

    result = asyncio.run(
        live._send_recovered_delivery_without_sid(
            {"quantity": "1"},
            order_id=f"order-compensation-{disposition}",
            item_id=ITEM_ID,
            buyer_id="buyer-1",
            source="test-compensation",
        )
    )

    assert result is expected_result
    assert logs
    assert {entry["status"] for entry in logs} == {"skipped"}


@pytest.mark.parametrize("entry", ["simple", "compensation"])
def test_real_delivery_entries_persist_only_sanitized_finalization_meta(
    runtime,
    monkeypatch,
    entry,
):
    live, manager = runtime
    create_binding(manager)
    live._order_locks = defaultdict(asyncio.Lock)
    live._lock_usage_times = {}
    order_id = f"order-sanitized-{entry}"
    raw_result = {
        "success": True,
        "configured": True,
        "content": "raw-content-secret",
        "contents": ["raw-content-secret"],
        "delivery_steps": [{"type": "text", "content": "raw-step-secret"}],
        "config": {"token": "provider-config-secret"},
        "provider_secret": "provider-secret",
        "card_id": 99,
        "mode": "fixed_link",
        "quantity": 1,
        "order_id": order_id,
        "order_line_id": ITEM_ID,
        "idempotency_key": "sanitized-key",
        "orchestration_status": "sending",
        "content_count": 1,
        "orchestration_meta": {"content_count": 1, "state": None},
        "delivery_unit_index": 1,
        "_orchestration_private": {
            "claim_token": "approved-claim-token",
            "provider_secret": "private-provider-secret",
        },
    }
    finalized_meta = []

    monkeypatch.setattr(live, "can_auto_delivery", lambda value: True)
    monkeypatch.setattr(live, "is_lock_held", lambda value: False)
    monkeypatch.setattr(live, "_get_pending_delivery_finalization_meta", lambda *args: None)
    monkeypatch.setattr(
        manager,
        "get_order_by_id",
        lambda value: {"order_id": value, "quantity": "1"},
    )

    async def return_raw_result(*args, **kwargs):
        return dict(raw_result)

    async def finalize_without_network(**kwargs):
        finalized_meta.append(dict(kwargs["delivery_meta"]))
        return {"success": True}

    async def ignore_async(*args, **kwargs):
        return None

    monkeypatch.setattr(live, "_auto_delivery", return_raw_result)
    monkeypatch.setattr(live, "_finalize_delivery_after_send", finalize_without_network)
    monkeypatch.setattr(live, "_mark_data_reservation_sent_if_needed", lambda meta: True)
    monkeypatch.setattr(live, "_sync_order_delivery_progress", lambda **kwargs: None)
    monkeypatch.setattr(live, "_activate_delivery_lock", lambda *args, **kwargs: None)
    monkeypatch.setattr(live, "_record_delivery_log", lambda **kwargs: None)
    monkeypatch.setattr(live, "send_delivery_failure_notification", ignore_async)

    if entry == "simple":
        async def owned_item(*args, **kwargs):
            return True

        monkeypatch.setattr(live, "_ensure_item_owned_by_current_account", owned_item)
        monkeypatch.setattr(
            live,
            "_check_buyer_blacklist_for_action",
            lambda **kwargs: False,
        )
        monkeypatch.setattr(live, "_send_delivery_steps", ignore_async)
        asyncio.run(
            live._handle_simple_message_auto_delivery(
                object(),
                order_id,
                ITEM_ID,
                "buyer-1",
                "chat-1",
                "now",
                "message-1",
            )
        )
    else:
        monkeypatch.setattr(live, "send_delivery_steps_once", ignore_async)
        assert asyncio.run(
            live._send_recovered_delivery_without_sid(
                {"quantity": "1"},
                order_id=order_id,
                item_id=ITEM_ID,
                buyer_id="buyer-1",
                source="test-sanitized-compensation",
            )
        ) is True

    stored_meta = manager.get_delivery_finalization_state(order_id, 1)["delivery_meta"]
    expected_meta = live._delivery_result_to_finalization_meta(raw_result)

    assert stored_meta == expected_meta
    assert finalized_meta == [expected_meta]
    assert set(stored_meta["_orchestration_private"]) == {"claim_token"}
    assert stored_meta["_orchestration_private"]["claim_token"] == "approved-claim-token"
    assert "claim_token" not in stored_meta
    for forbidden_key in (
        "content",
        "contents",
        "delivery_steps",
        "config",
        "provider_secret",
    ):
        assert forbidden_key not in stored_meta
