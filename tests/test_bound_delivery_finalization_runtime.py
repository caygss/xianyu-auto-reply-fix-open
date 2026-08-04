import asyncio
import time
from collections import defaultdict

import pytest

import XianyuAutoAsync as xianyu_module
import db_manager as db_manager_module
from card_inventory_service import CardInventoryService
from db_manager import DBManager
from delivery_config_service import DeliveryConfigService
from delivery_orchestration_service import DeliveryOrchestrationService
from delivery_orchestration_service import DeliveryOrchestrationError


USER_ID = 1
ACCOUNT_ID = "account-a"
ITEM_ID = "item-bound"


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    manager = DBManager(str(tmp_path / "bound-delivery-finalization.sqlite3"))
    with manager.lock:
        manager.conn.execute(
            """
            INSERT OR IGNORE INTO users (id, username, email, password_hash)
            VALUES (?, ?, ?, ?)
            """,
            (USER_ID, "finalization-owner", "finalization@example.test", "test"),
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
    yield live, manager
    manager.close()


def create_binding(manager):
    return manager.get_or_create_item_delivery_card(
        USER_ID,
        ACCOUNT_ID,
        ITEM_ID,
        "绑定商品",
    )["card_id"]


def orchestration_state(manager, order_id):
    with manager.lock:
        row = manager.conn.execute(
            """
            SELECT status, claim_token, terminal_claim_token, reservation_id
            FROM delivery_orchestration_states
            WHERE order_id = ?
            """,
            (order_id,),
        ).fetchone()
    return tuple(row) if row else None


def test_fixed_link_finalize_marks_orchestration_sent(runtime):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "fixed_link",
        {"url": "https://example.test/finalized"},
    )

    prepared = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            "order-fixed-finalized",
            "buyer-1",
            include_meta=True,
            order_line_id=ITEM_ID,
        )
    )
    claim_token = prepared["_orchestration_private"]["claim_token"]
    delivery_meta = live._delivery_result_to_finalization_meta(prepared)

    result = asyncio.run(
        live._finalize_delivery_after_send(
            delivery_meta=delivery_meta,
            order_id="order-fixed-finalized",
            item_id=ITEM_ID,
            skip_confirm=True,
        )
    )
    repeated_result = asyncio.run(
        live._finalize_delivery_after_send(
            delivery_meta=delivery_meta,
            order_id="order-fixed-finalized",
            item_id=ITEM_ID,
            skip_confirm=True,
        )
    )

    assert result["success"] is True
    assert repeated_result["success"] is True
    assert orchestration_state(manager, "order-fixed-finalized") == (
        "sent",
        None,
        claim_token,
        None,
    )


def test_configured_send_failure_retries_same_reservation_and_contents(runtime):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "imported_card",
        {"source": "send-failure-retry"},
    )
    inventory = CardInventoryService(manager)
    inventory.save_settings(card_id, USER_ID, ACCOUNT_ID, stock_ceiling=2)
    inventory.import_items(
        card_id,
        USER_ID,
        ACCOUNT_ID,
        ["retry-secret-a", "retry-secret-b"],
    )
    call = dict(
        item_id=ITEM_ID,
        item_title="绑定商品",
        order_id="order-send-retry",
        send_user_id="buyer-1",
        include_meta=True,
        quantity=2,
        order_line_id=ITEM_ID,
    )

    first = asyncio.run(live._auto_delivery(**call))
    first_meta = live._delivery_result_to_finalization_meta(first)
    first_reservation = first["reservation_id"]
    first_contents = list(first["contents"])

    live._mark_configured_delivery_failed(
        first_meta,
        RuntimeError("external sender failed"),
        order_id="order-send-retry",
        item_id=ITEM_ID,
    )
    retried = asyncio.run(live._auto_delivery(**call))

    assert retried["success"] is True
    assert retried["reservation_id"] == first_reservation
    assert retried["contents"] == first_contents
    assert retried["_orchestration_private"]["claim_token"] != (
        first["_orchestration_private"]["claim_token"]
    )
    with manager.lock:
        reservation_count = manager.conn.execute(
            "SELECT COUNT(*) FROM card_inventory_reservations WHERE order_id = ?",
            ("order-send-retry",),
        ).fetchone()[0]
    assert reservation_count == 1


def test_slow_external_send_renews_claim_and_leaves_no_heartbeat_task(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    live.delivery_claim_lease_seconds = 1
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "fixed_link",
        {"url": "https://example.test/slow-send"},
    )
    call = dict(
        item_id=ITEM_ID,
        item_title="绑定商品",
        order_id="order-slow-send",
        send_user_id="buyer-1",
        include_meta=True,
        order_line_id=ITEM_ID,
    )
    prepared = asyncio.run(live._auto_delivery(**call))
    delivery_meta = live._delivery_result_to_finalization_meta(prepared)
    renewal_times = []
    real_renew_claim = DeliveryOrchestrationService.renew_claim

    def record_renewal(service, request, claim_token):
        renewal_times.append(time.monotonic())
        return real_renew_claim(service, request, claim_token)

    monkeypatch.setattr(
        DeliveryOrchestrationService,
        "renew_claim",
        record_renewal,
    )

    async def exercise():
        async with live._delivery_claim_heartbeat(
            delivery_meta,
            order_id="order-slow-send",
            item_id=ITEM_ID,
        ):
            await asyncio.sleep(1.15)
            repeated = await live._auto_delivery(**call)
        leaked = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("delivery-claim-heartbeat-")
            and not task.done()
        ]
        return repeated, leaked

    repeated, leaked = asyncio.run(exercise())

    assert repeated["disposition"] == "defer_in_progress"
    assert len(renewal_times) >= 3
    assert all(
        later - earlier <= 0.45
        for earlier, later in zip(renewal_times, renewal_times[1:])
    )
    assert leaked == []


@pytest.mark.parametrize("outcome", ["error", "cancel"])
def test_send_claim_wrapper_stops_heartbeat_on_error_or_cancellation(
    runtime,
    outcome,
):
    live, manager = runtime
    live.delivery_claim_lease_seconds = 1
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "fixed_link",
        {"url": "https://example.test/heartbeat-cleanup"},
    )
    prepared = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            f"order-heartbeat-{outcome}",
            "buyer-1",
            include_meta=True,
            order_line_id=ITEM_ID,
        )
    )
    delivery_meta = live._delivery_result_to_finalization_meta(prepared)

    async def exercise():
        async def sender():
            if outcome == "error":
                raise RuntimeError("sender failed")
            await asyncio.Event().wait()

        send_task = asyncio.create_task(
            live._send_with_delivery_claim(
                delivery_meta,
                sender,
                order_id=f"order-heartbeat-{outcome}",
                item_id=ITEM_ID,
            )
        )
        await asyncio.sleep(0)
        if outcome == "cancel":
            send_task.cancel()
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await send_task
        await asyncio.sleep(0)
        return [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("delivery-claim-heartbeat-")
            and not task.done()
        ]

    assert asyncio.run(exercise()) == []


def test_main_auto_delivery_conflict_has_zero_prepare_send_or_success_notification(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "imported_card",
        {"source": "main-conflict"},
    )
    inventory = CardInventoryService(manager)
    inventory.save_settings(card_id, USER_ID, ACCOUNT_ID, stock_ceiling=1)
    inventory.import_items(card_id, USER_ID, ACCOUNT_ID, ["must-remain-available"])
    order_id = "order-main-conflict"
    binding = live._get_bound_item_delivery(ITEM_ID)
    preparation_plan = live._build_auto_delivery_preparation_plan(
        ITEM_ID,
        1,
        item_delivery_binding=binding,
    )
    live._order_locks = defaultdict(asyncio.Lock)
    live._lock_usage_times = {}
    send_calls = []
    notifications = []

    async def owned_item(*args, **kwargs):
        return True

    async def build_plan(*args, **kwargs):
        return 1, preparation_plan

    async def record_send(*args, **kwargs):
        send_calls.append((args, kwargs))

    async def record_notification(*args, **kwargs):
        notifications.append((args, kwargs))

    monkeypatch.setattr(live, "_ensure_item_owned_by_current_account", owned_item)
    monkeypatch.setattr(live, "_check_buyer_blacklist_for_action", lambda **kwargs: False)
    monkeypatch.setattr(live, "_extract_order_id", lambda *args: order_id)
    monkeypatch.setattr(live, "_is_trustworthy_buyer_id", lambda value: False)
    monkeypatch.setattr(live, "is_lock_held", lambda value: False)
    monkeypatch.setattr(live, "can_auto_delivery", lambda value: True)
    monkeypatch.setattr(live, "_build_order_auto_delivery_preparation_plan", build_plan)
    monkeypatch.setattr(live, "_send_delivery_steps", record_send)
    monkeypatch.setattr(live, "send_delivery_failure_notification", record_notification)
    monkeypatch.setattr(live, "_record_delivery_log", lambda **kwargs: None)
    monkeypatch.setattr(
        manager,
        "get_order_by_id",
        lambda value: {
            "order_id": value,
            "buyer_id": "buyer-1",
            "item_id": ITEM_ID,
            "quantity": "1",
        },
    )
    monkeypatch.setattr(
        manager,
        "get_delivery_progress_summary",
        lambda *args, **kwargs: {
            "coverage_conflict": True,
            "conflict_unit_indexes": [1],
            "aggregate_status": "conflict",
            "finalized_count": 0,
            "pending_finalize_count": 0,
            "remaining_count": 1,
        },
    )

    asyncio.run(
        live._handle_auto_delivery(
            object(),
            {},
            "买家",
            "buyer-1",
            ITEM_ID,
            "chat-1",
            "now",
        )
    )

    summary = inventory.get_inventory_summary(card_id, USER_ID, ACCOUNT_ID)
    with manager.lock:
        orchestration_count = manager.conn.execute(
            "SELECT COUNT(*) FROM delivery_orchestration_states WHERE order_id = ?",
            (order_id,),
        ).fetchone()[0]
    assert summary["available"] == 1
    assert summary["sent"] == 0
    assert orchestration_count == 0
    assert send_calls == []
    assert notifications == []


def test_main_auto_delivery_wraps_external_send_with_claim(runtime, monkeypatch):
    live, manager = runtime
    order_id = "order-main-heartbeat"
    live._order_locks = defaultdict(asyncio.Lock)
    live._lock_usage_times = {}
    wrapped_sends = []
    send_calls = []
    raw_result = {
        "success": True,
        "configured": True,
        "content": "configured-content",
        "contents": ["configured-content"],
        "delivery_steps": [{"type": "text", "content": "configured-content"}],
        "card_id": 7,
        "card_type": "text",
        "mode": "fixed_link",
        "quantity": 1,
        "order_id": order_id,
        "order_line_id": ITEM_ID,
        "idempotency_key": "canonical-key",
        "orchestration_status": "sending",
        "content_count": 1,
        "delivery_unit_index": 1,
        "_orchestration_private": {"claim_token": "private-claim"},
    }

    async def return_plan(*args, **kwargs):
        return 1, [{
            "unit_index": 1,
            "quantity": 1,
            "order_line_id": ITEM_ID,
            "binding_snapshot": {},
        }]

    async def return_delivery(*args, **kwargs):
        return dict(raw_result)

    async def record_send(*args, **kwargs):
        send_calls.append((args, kwargs))

    async def record_wrapper(delivery_meta, sender, **kwargs):
        wrapped_sends.append(dict(delivery_meta))
        return await sender()

    async def ignore_async(*args, **kwargs):
        return None

    monkeypatch.setattr(live, "_check_buyer_blacklist_for_action", lambda **kwargs: False)
    monkeypatch.setattr(live, "_extract_order_id", lambda *args: order_id)
    monkeypatch.setattr(live, "_is_trustworthy_buyer_id", lambda value: False)
    monkeypatch.setattr(live, "is_lock_held", lambda value: False)
    monkeypatch.setattr(live, "can_auto_delivery", lambda value: True)
    monkeypatch.setattr(live, "_build_order_auto_delivery_preparation_plan", return_plan)
    monkeypatch.setattr(live, "_auto_delivery", return_delivery)
    monkeypatch.setattr(live, "_send_delivery_steps", record_send)
    monkeypatch.setattr(live, "_send_with_delivery_claim", record_wrapper)
    monkeypatch.setattr(live, "_finalize_delivery_after_send", lambda **kwargs: ignore_async())
    monkeypatch.setattr(live, "_mark_data_reservation_sent_if_needed", lambda meta: True)
    monkeypatch.setattr(live, "_sync_order_delivery_progress", lambda **kwargs: {
        "aggregate_status": "shipped",
        "finalized_count": 1,
        "pending_finalize_count": 0,
        "remaining_count": 0,
    })
    monkeypatch.setattr(live, "_activate_delivery_lock", lambda *args, **kwargs: None)
    monkeypatch.setattr(live, "_record_delivery_log", lambda **kwargs: None)
    monkeypatch.setattr(live, "send_delivery_failure_notification", ignore_async)
    monkeypatch.setattr(
        manager,
        "get_order_by_id",
        lambda value: {
            "order_id": value,
            "buyer_id": "buyer-1",
            "item_id": ITEM_ID,
            "quantity": "1",
        },
    )
    monkeypatch.setattr(
        manager,
        "get_delivery_progress_summary",
        lambda *args, **kwargs: {
            "coverage_conflict": False,
            "aggregate_status": "pending",
        },
    )

    asyncio.run(
        live._handle_auto_delivery(
            object(),
            {},
            "买家",
            "buyer-1",
            "未知商品",
            "chat-1",
            "now",
        )
    )

    assert len(send_calls) == 1
    assert wrapped_sends == [live._delivery_result_to_finalization_meta(raw_result)]


def test_crash_recovery_finalizes_batch_without_resending(runtime, monkeypatch):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "imported_card",
        {"source": "crash-recovery"},
    )
    inventory = CardInventoryService(manager)
    inventory.save_settings(card_id, USER_ID, ACCOUNT_ID, stock_ceiling=3)
    inventory.import_items(
        card_id,
        USER_ID,
        ACCOUNT_ID,
        ["crash-a", "crash-b", "crash-c"],
    )
    order_id = "order-crash-recovery"
    manager.insert_or_update_order(
        order_id=order_id,
        item_id=ITEM_ID,
        buyer_id="buyer-1",
        quantity="3",
        order_status="pending_ship",
        cookie_id=ACCOUNT_ID,
    )
    prepared = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            order_id,
            "buyer-1",
            include_meta=True,
            quantity=3,
            order_line_id=ITEM_ID,
        )
    )
    delivery_meta = live._delivery_result_to_finalization_meta(prepared)
    live._persist_delivery_finalization_state(
        order_id=order_id,
        item_id=ITEM_ID,
        buyer_id="buyer-1",
        delivery_meta=delivery_meta,
        channel="auto",
        status="sent",
    )
    assert orchestration_state(manager, order_id)[0] == "sending"

    live._order_locks = defaultdict(asyncio.Lock)
    live._lock_usage_times = {}
    live.delivery_sent_orders = set()
    live.last_delivery_time = {}
    live.order_status_handler = None
    live.confirmed_orders = {}
    live.order_confirm_cooldown = 300
    confirm_calls = []
    synchronized_quantities = []
    real_sync = live._sync_order_delivery_progress

    async def fail_if_resent(*args, **kwargs):
        raise AssertionError("crash recovery must not resend delivery content")

    async def confirm_once(*args, **kwargs):
        confirm_calls.append(orchestration_state(manager, order_id)[0])
        return {"success": True}

    def record_sync(**kwargs):
        synchronized_quantities.append(kwargs["expected_quantity"])
        return real_sync(**kwargs)

    monkeypatch.setattr(live, "can_auto_delivery", lambda value: True)
    monkeypatch.setattr(live, "is_lock_held", lambda value: False)
    monkeypatch.setattr(live, "is_auto_confirm_enabled", lambda: True)
    monkeypatch.setattr(live, "auto_confirm", confirm_once)
    monkeypatch.setattr(live, "send_delivery_steps_once", fail_if_resent)
    monkeypatch.setattr(live, "_sync_order_delivery_progress", record_sync)
    monkeypatch.setattr(live, "_activate_delivery_lock", lambda *args, **kwargs: None)
    monkeypatch.setattr(live, "_record_delivery_log", lambda **kwargs: None)
    monkeypatch.setattr(
        live,
        "_notify_republish_after_delivery_finalized",
        lambda **kwargs: None,
    )

    recovered = asyncio.run(
        live._send_recovered_delivery_without_sid(
            {"quantity": "3"},
            order_id=order_id,
            item_id=ITEM_ID,
            buyer_id="buyer-1",
            source="test-crash-recovery",
        )
    )

    summary = manager.get_delivery_progress_summary(order_id, expected_quantity=3)
    finalization_states = manager.get_delivery_finalization_states(order_id)
    assert recovered is True
    assert confirm_calls == ["sent"]
    assert synchronized_quantities == [3]
    assert orchestration_state(manager, order_id)[0] == "sent"
    assert len(finalization_states) == 1
    assert finalization_states[0]["status"] == "finalized"
    assert summary["finalized_count"] == 3
    assert summary["aggregate_status"] == "shipped"
    assert manager.get_order_by_id(order_id)["order_status"] == "shipped"


def test_simple_entry_finalizes_quantity_three_as_one_batch(runtime, monkeypatch):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "imported_card",
        {"source": "simple-batch"},
    )
    inventory = CardInventoryService(manager)
    inventory.save_settings(card_id, USER_ID, ACCOUNT_ID, stock_ceiling=3)
    inventory.import_items(
        card_id,
        USER_ID,
        ACCOUNT_ID,
        ["simple-a", "simple-b", "simple-c"],
    )
    order_id = "order-simple-batch"
    manager.insert_or_update_order(
        order_id=order_id,
        item_id=ITEM_ID,
        buyer_id="buyer-1",
        quantity="3",
        order_status="pending_ship",
        cookie_id=ACCOUNT_ID,
    )
    live._order_locks = defaultdict(asyncio.Lock)
    live._lock_usage_times = {}
    live.delivery_sent_orders = set()
    live.last_delivery_time = {}
    live.order_status_handler = None
    live.confirmed_orders = {}
    live.order_confirm_cooldown = 300
    send_calls = []
    confirm_calls = []
    synchronized_quantities = []
    mark_sent_quantities = []
    real_sync = live._sync_order_delivery_progress
    real_mark_sent = DeliveryOrchestrationService.mark_sent

    async def owned_item(*args, **kwargs):
        return True

    async def record_send(*args, **kwargs):
        send_calls.append((args, kwargs))

    async def confirm_once(*args, **kwargs):
        confirm_calls.append(orchestration_state(manager, order_id)[0])
        return {"success": True}

    async def ignore_async(*args, **kwargs):
        return None

    def record_sync(**kwargs):
        synchronized_quantities.append(kwargs["expected_quantity"])
        return real_sync(**kwargs)

    def record_mark_sent(service, request, claim_token):
        mark_sent_quantities.append(request.quantity)
        return real_mark_sent(service, request, claim_token)

    monkeypatch.setattr(live, "_ensure_item_owned_by_current_account", owned_item)
    monkeypatch.setattr(live, "_check_buyer_blacklist_for_action", lambda **kwargs: False)
    monkeypatch.setattr(live, "can_auto_delivery", lambda value: True)
    monkeypatch.setattr(live, "is_lock_held", lambda value: False)
    monkeypatch.setattr(live, "is_auto_confirm_enabled", lambda: True)
    monkeypatch.setattr(live, "auto_confirm", confirm_once)
    monkeypatch.setattr(live, "_send_delivery_steps", record_send)
    monkeypatch.setattr(live, "_sync_order_delivery_progress", record_sync)
    monkeypatch.setattr(
        DeliveryOrchestrationService,
        "mark_sent",
        record_mark_sent,
    )
    monkeypatch.setattr(live, "_activate_delivery_lock", lambda *args, **kwargs: None)
    monkeypatch.setattr(live, "_record_delivery_log", lambda **kwargs: None)
    monkeypatch.setattr(live, "send_delivery_failure_notification", ignore_async)
    monkeypatch.setattr(
        live,
        "_notify_republish_after_delivery_finalized",
        lambda **kwargs: None,
    )

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

    summary = manager.get_delivery_progress_summary(order_id, expected_quantity=3)
    finalization_states = manager.get_delivery_finalization_states(order_id)
    assert len(send_calls) == 1
    assert confirm_calls == ["sent"]
    assert mark_sent_quantities == [3]
    assert synchronized_quantities == [3]
    assert len(finalization_states) == 1
    assert finalization_states[0]["status"] == "finalized"
    assert summary["finalized_count"] == 3
    assert summary["aggregate_status"] == "shipped"
    assert manager.get_order_by_id(order_id)["order_status"] == "shipped"


def test_platform_confirm_failure_keeps_orchestration_sent_and_blocks_resend(
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
        {"url": "https://example.test/confirm-failure"},
    )
    order_id = "order-confirm-failure"
    call = dict(
        item_id=ITEM_ID,
        item_title="绑定商品",
        order_id=order_id,
        send_user_id="buyer-1",
        include_meta=True,
        order_line_id=ITEM_ID,
    )
    prepared = asyncio.run(live._auto_delivery(**call))
    delivery_meta = live._delivery_result_to_finalization_meta(prepared)
    live.confirmed_orders = {}
    live.order_confirm_cooldown = 300

    async def fail_confirm(*args, **kwargs):
        assert orchestration_state(manager, order_id)[0] == "sent"
        return {"success": False, "error": "platform unavailable"}

    monkeypatch.setattr(live, "is_auto_confirm_enabled", lambda: True)
    monkeypatch.setattr(live, "auto_confirm", fail_confirm)

    finalized = asyncio.run(
        live._finalize_delivery_after_send(
            delivery_meta=delivery_meta,
            order_id=order_id,
            item_id=ITEM_ID,
        )
    )
    repeated = asyncio.run(live._auto_delivery(**call))

    assert finalized["success"] is False
    assert finalized["platform_confirm_failed"] is True
    assert orchestration_state(manager, order_id)[0] == "sent"
    assert repeated["disposition"] == "noop_sent"
    assert repeated["content"] is None


def test_shortage_then_restock_retries_whole_configured_order(runtime, monkeypatch):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "imported_card",
        {"source": "shortage-recovery"},
    )
    inventory = CardInventoryService(manager)
    inventory.save_settings(card_id, USER_ID, ACCOUNT_ID, stock_ceiling=2)
    inventory.import_items(card_id, USER_ID, ACCOUNT_ID, ["restock-a"])
    order_id = "order-shortage-recovery"
    manager.insert_or_update_order(
        order_id=order_id,
        item_id=ITEM_ID,
        buyer_id="buyer-1",
        quantity="2",
        order_status="pending_ship",
        cookie_id=ACCOUNT_ID,
    )
    live._order_locks = defaultdict(asyncio.Lock)
    live._lock_usage_times = {}
    live.delivery_sent_orders = set()
    live.last_delivery_time = {}
    live.order_status_handler = None
    live.confirmed_orders = {}
    live.order_confirm_cooldown = 300
    sent_batches = []
    confirm_calls = []
    prepare_results = []
    mark_sent_quantities = []
    real_prepare_retry = DeliveryOrchestrationService.prepare_retry
    real_mark_sent = DeliveryOrchestrationService.mark_sent

    async def owned_item(*args, **kwargs):
        return True

    async def record_send(websocket, chat_id, user_id, delivery_steps, **kwargs):
        sent_batches.append([step["content"] for step in delivery_steps])

    async def confirm_once(*args, **kwargs):
        confirm_calls.append(orchestration_state(manager, order_id)[0])
        return {"success": True}

    async def ignore_async(*args, **kwargs):
        return None

    def record_prepare_retry(service, request):
        result = real_prepare_retry(service, request)
        prepare_results.append(
            (
                request.quantity,
                result["status"],
                result.get("reservation_id"),
                list(result.get("contents") or []),
            )
        )
        return result

    def record_mark_sent(service, request, claim_token):
        mark_sent_quantities.append(request.quantity)
        return real_mark_sent(service, request, claim_token)

    monkeypatch.setattr(live, "_ensure_item_owned_by_current_account", owned_item)
    monkeypatch.setattr(live, "_check_buyer_blacklist_for_action", lambda **kwargs: False)
    monkeypatch.setattr(live, "can_auto_delivery", lambda value: True)
    monkeypatch.setattr(live, "is_lock_held", lambda value: False)
    monkeypatch.setattr(live, "is_auto_confirm_enabled", lambda: True)
    monkeypatch.setattr(live, "auto_confirm", confirm_once)
    monkeypatch.setattr(live, "_send_delivery_steps", record_send)
    monkeypatch.setattr(live, "_activate_delivery_lock", lambda *args, **kwargs: None)
    monkeypatch.setattr(live, "_record_delivery_log", lambda **kwargs: None)
    monkeypatch.setattr(live, "send_delivery_failure_notification", ignore_async)
    monkeypatch.setattr(
        live,
        "_notify_republish_after_delivery_finalized",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        DeliveryOrchestrationService,
        "prepare_retry",
        record_prepare_retry,
    )
    monkeypatch.setattr(
        DeliveryOrchestrationService,
        "mark_sent",
        record_mark_sent,
    )

    asyncio.run(
        live._handle_simple_message_auto_delivery(
            object(),
            order_id,
            ITEM_ID,
            "buyer-1",
            "chat-1",
            "now",
            "message-shortage",
        )
    )

    shortage_state = orchestration_state(manager, order_id)
    shortage_inventory = inventory.get_inventory_summary(card_id, USER_ID, ACCOUNT_ID)
    with manager.lock:
        shortage_reservation_count = manager.conn.execute(
            "SELECT COUNT(*) FROM card_inventory_reservations WHERE order_id = ?",
            (order_id,),
        ).fetchone()[0]

    assert shortage_state[0] == "paused"
    assert shortage_state[3] is None
    assert shortage_inventory["available"] == 1
    assert shortage_inventory["sent"] == 0
    assert shortage_reservation_count == 0
    assert sent_batches == []
    assert confirm_calls == []

    inventory.import_items(card_id, USER_ID, ACCOUNT_ID, ["restock-b"])
    asyncio.run(
        live._handle_simple_message_auto_delivery(
            object(),
            order_id,
            ITEM_ID,
            "buyer-1",
            "chat-1",
            "now",
            "message-restocked",
        )
    )

    summary = manager.get_delivery_progress_summary(order_id, expected_quantity=2)
    finalization_states = manager.get_delivery_finalization_states(order_id)
    final_inventory = inventory.get_inventory_summary(card_id, USER_ID, ACCOUNT_ID)
    with manager.lock:
        state_count = manager.conn.execute(
            "SELECT COUNT(*) FROM delivery_orchestration_states WHERE order_id = ?",
            (order_id,),
        ).fetchone()[0]
        reservation_count = manager.conn.execute(
            "SELECT COUNT(*) FROM card_inventory_reservations WHERE order_id = ?",
            (order_id,),
        ).fetchone()[0]

    assert prepare_results[0] == (2, "paused", None, [])
    assert prepare_results[1][0:2] == (2, "sending")
    assert prepare_results[1][3] == ["restock-a", "restock-b"]
    assert sent_batches == [["restock-a", "restock-b"]]
    assert mark_sent_quantities == [2]
    assert confirm_calls == ["sent"]
    assert orchestration_state(manager, order_id)[0] == "sent"
    assert len(finalization_states) == 1
    assert finalization_states[0]["status"] == "finalized"
    assert summary["finalized_count"] == 2
    assert summary["pending_finalize_count"] == 0
    assert summary["remaining_count"] == 0
    assert summary["aggregate_status"] == "shipped"
    assert manager.get_order_by_id(order_id)["order_status"] == "shipped"
    assert final_inventory["available"] == 0
    assert final_inventory["sent"] == 2
    assert state_count == 1
    assert reservation_count == 1


def test_stale_failure_token_cannot_overwrite_new_claim(runtime):
    live, manager = runtime
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "fixed_link",
        {"url": "https://example.test/stale-token"},
    )
    call = dict(
        item_id=ITEM_ID,
        item_title="绑定商品",
        order_id="order-stale-token",
        send_user_id="buyer-1",
        include_meta=True,
        order_line_id=ITEM_ID,
    )
    first = asyncio.run(live._auto_delivery(**call))
    first_meta = live._delivery_result_to_finalization_meta(first)
    live._mark_configured_delivery_failed(
        first_meta,
        RuntimeError("first sender failed"),
        order_id="order-stale-token",
        item_id=ITEM_ID,
    )
    retried = asyncio.run(live._auto_delivery(**call))

    with pytest.raises(DeliveryOrchestrationError) as stale_error:
        live._mark_configured_delivery_failed(
            first_meta,
            RuntimeError("stale sender failed"),
            order_id="order-stale-token",
            item_id=ITEM_ID,
        )

    assert stale_error.value.code == "claim_token_mismatch"
    assert first["_orchestration_private"]["claim_token"] not in str(stale_error.value)
    state = orchestration_state(manager, "order-stale-token")
    assert state[0] == "sending"
    assert state[1] == retried["_orchestration_private"]["claim_token"]
