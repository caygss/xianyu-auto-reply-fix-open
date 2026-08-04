import asyncio
import threading
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


def orchestration_send_safety(manager, order_id):
    with manager.lock:
        row = manager.conn.execute(
            """
            SELECT item_id, send_started_at, verification_required
            FROM delivery_orchestration_states
            WHERE order_id = ?
            """,
            (order_id,),
        ).fetchone()
    return tuple(row) if row else None


def prepare_fixed_link_claim(live, manager, order_id):
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "fixed_link",
        {"url": f"https://example.test/{order_id}"},
    )
    prepared = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            order_id,
            "buyer-1",
            include_meta=True,
            order_line_id=ITEM_ID,
        )
    )
    assert prepared["success"] is True
    return live._delivery_result_to_finalization_meta(prepared)


def test_historical_verification_anchor_is_not_eligible_for_auto_finalize(runtime):
    live, manager = runtime
    order_id = "order-historical-verification-anchor"
    private_token = "historical-private-claim-token"
    historical_meta = {
        "success": True,
        "configured": True,
        "claim_verification_required": True,
        "idempotency_key": "historical-verification-key",
        "_orchestration_private": {"claim_token": private_token},
    }
    assert live._persist_delivery_finalization_state(
        order_id=order_id,
        item_id=ITEM_ID,
        buyer_id="buyer-1",
        delivery_meta=historical_meta,
        channel="auto",
        status="sent",
    ) is True

    assert live._get_pending_delivery_finalization_meta(order_id, 1) is None
    stored = manager.get_delivery_finalization_state(order_id, 1)
    assert stored["status"] == "sent"
    assert stored["delivery_meta"]["claim_verification_required"] is True


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

    asyncio.run(
        live._mark_configured_delivery_failed(
            first_meta,
            RuntimeError("external sender failed"),
            order_id="order-send-retry",
            item_id=ITEM_ID,
        )
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

    assert repeated["disposition"] == "verification_required"
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
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await send_task
        else:
            with pytest.raises(RuntimeError, match="sender failed"):
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


@pytest.mark.parametrize("begin_outcome", ["false", "error"])
def test_configured_send_prewrite_failure_never_starts_sender(
    runtime,
    monkeypatch,
    begin_outcome,
):
    live, manager = runtime
    order_id = f"order-begin-send-{begin_outcome}"
    delivery_meta = prepare_fixed_link_claim(live, manager, order_id)
    sender_calls = []

    def fail_begin_send(service, request, claim_token):
        if begin_outcome == "error":
            raise RuntimeError("begin send storage failed")
        return False

    async def sender():
        sender_calls.append(True)
        return "must-not-send"

    monkeypatch.setattr(
        DeliveryOrchestrationService,
        "begin_send",
        fail_begin_send,
    )

    with pytest.raises(xianyu_module.DeliveryClaimOwnershipError) as error_info:
        asyncio.run(
            live._send_with_delivery_claim(
                delivery_meta,
                sender,
                order_id=order_id,
                item_id=ITEM_ID,
            )
        )

    assert sender_calls == []
    assert delivery_meta["_orchestration_private"]["claim_token"] not in str(
        error_info.value
    )


@pytest.mark.parametrize("renewal_outcome", ["error", "lost"])
def test_unfinished_sender_heartbeat_failure_requires_verification_and_never_reclaims(
    runtime,
    monkeypatch,
    renewal_outcome,
):
    live, manager = runtime
    live.delivery_claim_lease_seconds = 1
    order_id = f"order-unfinished-{renewal_outcome}-verification"
    delivery_meta = prepare_fixed_link_claim(live, manager, order_id)
    sender_calls = []

    def fail_renewal(service, request, claim_token):
        if renewal_outcome == "error":
            raise RuntimeError("heartbeat storage failed")
        return False

    async def sender():
        sender_calls.append(True)
        await asyncio.Event().wait()

    monkeypatch.setattr(
        DeliveryOrchestrationService,
        "renew_claim",
        fail_renewal,
    )

    with pytest.raises(xianyu_module.DeliveryClaimUncertainError):
        asyncio.run(
            live._send_with_delivery_claim(
                delivery_meta,
                sender,
                order_id=order_id,
                item_id=ITEM_ID,
            )
        )

    time.sleep(1.05)
    retried = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            order_id,
            "buyer-1",
            include_meta=True,
            order_line_id=ITEM_ID,
        )
    )

    assert sender_calls == [True]
    assert manager.get_delivery_finalization_state(order_id, 1) is None
    item_id, send_started_at, verification_required = orchestration_send_safety(
        manager,
        order_id,
    )
    assert item_id == ITEM_ID
    assert send_started_at is not None
    assert verification_required == 1
    assert retried["disposition"] == "verification_required"
    assert retried["content"] is None


@pytest.mark.parametrize("renewal_outcome", ["error", "lost"])
def test_heartbeat_failure_after_sender_completed_persists_sent_anchor(
    runtime,
    monkeypatch,
    renewal_outcome,
):
    live, manager = runtime
    live.delivery_claim_lease_seconds = 1
    order_id = f"order-heartbeat-{renewal_outcome}-fail-safe"
    delivery_meta = prepare_fixed_link_claim(live, manager, order_id)
    private_token = delivery_meta["_orchestration_private"]["claim_token"]
    renewal_attempted = threading.Event()
    def fail_renewal(service, request, claim_token):
        renewal_attempted.set()
        if renewal_outcome == "error":
            raise RuntimeError("database heartbeat exploded")
        return False

    monkeypatch.setattr(
        DeliveryOrchestrationService,
        "renew_claim",
        fail_renewal,
    )

    async def exercise():
        async def sender():
            await asyncio.to_thread(renewal_attempted.wait)
            await asyncio.sleep(0)
            return "sender-completed"

        result = await live._send_with_delivery_claim(
            delivery_meta,
            sender,
            order_id=order_id,
            item_id=ITEM_ID,
        )

        finalization = manager.get_delivery_finalization_state(order_id, 1)
        assert finalization["status"] == "sent"
        assert "claim_verification_required" not in finalization["delivery_meta"]
        assert (
            finalization["delivery_meta"]["delivery_claim_status"]
            == "send_completed_pending_finalize"
        )
        await asyncio.sleep(0)
        leaked = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("delivery-claim-heartbeat-")
            and not task.done()
        ]
        return result, leaked

    result, leaked = asyncio.run(exercise())

    assert orchestration_state(manager, order_id)[0] == "sending"
    assert result == "sender-completed"
    assert private_token not in manager.get_delivery_finalization_state(
        order_id,
        1,
    )["last_error"]
    assert leaked == []


@pytest.mark.parametrize("anchor_outcome", ["false", "error"])
def test_completed_sender_anchor_write_failure_never_reclaims_after_lease(
    runtime,
    monkeypatch,
    anchor_outcome,
):
    live, manager = runtime
    live.delivery_claim_lease_seconds = 1
    order_id = f"order-completed-anchor-{anchor_outcome}"
    delivery_meta = prepare_fixed_link_claim(live, manager, order_id)
    private_token = delivery_meta["_orchestration_private"]["claim_token"]
    renewal_attempted = threading.Event()
    sender_calls = []

    def fail_renewal(service, request, claim_token):
        renewal_attempted.set()
        return False

    def fail_anchor_write(*args, **kwargs):
        if anchor_outcome == "error":
            raise RuntimeError("finalization storage failed")
        return False

    async def sender():
        sender_calls.append(True)
        await asyncio.to_thread(renewal_attempted.wait)
        await asyncio.sleep(0)
        return "sender-completed"

    monkeypatch.setattr(
        DeliveryOrchestrationService,
        "renew_claim",
        fail_renewal,
    )
    monkeypatch.setattr(
        live,
        "_persist_delivery_finalization_state",
        fail_anchor_write,
    )

    with pytest.raises(xianyu_module.DeliveryClaimUncertainError) as error_info:
        asyncio.run(
            live._send_with_delivery_claim(
                delivery_meta,
                sender,
                order_id=order_id,
                item_id=ITEM_ID,
            )
        )

    time.sleep(1.05)
    retried = asyncio.run(
        live._auto_delivery(
            ITEM_ID,
            "绑定商品",
            order_id,
            "buyer-1",
            include_meta=True,
            order_line_id=ITEM_ID,
        )
    )

    assert sender_calls == [True]
    assert manager.get_delivery_finalization_state(order_id, 1) is None
    _, send_started_at, verification_required = orchestration_send_safety(
        manager,
        order_id,
    )
    assert send_started_at is not None
    assert verification_required == 1
    assert retried["disposition"] == "verification_required"
    assert retried["content"] is None
    assert private_token not in str(error_info.value)


def test_cancellation_during_heartbeat_failure_propagates_only_cancelled_error(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    live.delivery_claim_lease_seconds = 1
    order_id = "order-heartbeat-cancel-priority"
    delivery_meta = prepare_fixed_link_claim(live, manager, order_id)
    renewal_started = threading.Event()
    renew_calls = 0
    real_renew_claim = DeliveryOrchestrationService.renew_claim

    def slow_failing_renewal(service, request, claim_token):
        nonlocal renew_calls
        renew_calls += 1
        if renew_calls == 1:
            return real_renew_claim(service, request, claim_token)
        renewal_started.set()
        time.sleep(0.15)
        raise RuntimeError("late heartbeat failure")

    monkeypatch.setattr(
        DeliveryOrchestrationService,
        "renew_claim",
        slow_failing_renewal,
    )

    async def exercise():
        sender_started = asyncio.Event()

        async def sender():
            sender_started.set()
            await asyncio.Event().wait()

        send_task = asyncio.create_task(
            live._send_with_delivery_claim(
                delivery_meta,
                sender,
                order_id=order_id,
                item_id=ITEM_ID,
            )
        )
        await sender_started.wait()
        while not renewal_started.is_set():
            await asyncio.sleep(0.01)
        send_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await send_task
        await asyncio.sleep(0)
        return [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("delivery-claim-heartbeat-")
            and not task.done()
        ]

    leaked = asyncio.run(exercise())
    assert manager.get_delivery_finalization_state(order_id, 1) is None
    assert orchestration_state(manager, order_id)[0] == "sending"
    _, send_started_at, verification_required = orchestration_send_safety(
        manager,
        order_id,
    )
    assert send_started_at is not None
    assert verification_required == 1
    assert leaked == []


def test_cancellation_never_calls_completed_sender_anchor_writer(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    order_id = "order-cancel-anchor-error"
    delivery_meta = prepare_fixed_link_claim(live, manager, order_id)

    completed_anchor_calls = []

    async def fail_anchor_write(*args, **kwargs):
        completed_anchor_calls.append((args, kwargs))
        raise RuntimeError("anchor write failed")

    monkeypatch.setattr(
        live,
        "_persist_completed_delivery_after_claim_loss",
        fail_anchor_write,
    )

    async def exercise():
        sender_started = asyncio.Event()

        async def sender():
            sender_started.set()
            await asyncio.Event().wait()

        send_task = asyncio.create_task(
            live._send_with_delivery_claim(
                delivery_meta,
                sender,
                order_id=order_id,
                item_id=ITEM_ID,
            )
        )
        await sender_started.wait()
        send_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await send_task
        await asyncio.sleep(0)
        return [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and (
                task.get_name().startswith("delivery-claim-heartbeat-")
                or getattr(task.get_coro(), "__qualname__", "").endswith(".sender")
            )
            and not task.done()
        ]

    assert asyncio.run(exercise()) == []
    assert completed_anchor_calls == []
    assert manager.get_delivery_finalization_state(order_id, 1) is None


def test_unfinished_sender_heartbeat_failure_never_calls_completed_anchor_writer(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    live.delivery_claim_lease_seconds = 1
    order_id = "order-heartbeat-anchor-error"
    delivery_meta = prepare_fixed_link_claim(live, manager, order_id)
    def fail_renewal(service, request, claim_token):
        raise RuntimeError("heartbeat failed")

    completed_anchor_calls = []

    async def fail_anchor_write(*args, **kwargs):
        completed_anchor_calls.append((args, kwargs))
        raise RuntimeError("anchor write failed")

    monkeypatch.setattr(
        DeliveryOrchestrationService,
        "renew_claim",
        fail_renewal,
    )
    monkeypatch.setattr(
        live,
        "_persist_completed_delivery_after_claim_loss",
        fail_anchor_write,
    )

    async def exercise():
        async def sender():
            await asyncio.Event().wait()

        with pytest.raises(xianyu_module.DeliveryClaimUncertainError):
            await live._send_with_delivery_claim(
                delivery_meta,
                sender,
                order_id=order_id,
                item_id=ITEM_ID,
            )
        await asyncio.sleep(0)
        return [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and (
                task.get_name().startswith("delivery-claim-heartbeat-")
                or getattr(task.get_coro(), "__qualname__", "").endswith(".sender")
            )
            and not task.done()
        ]

    assert asyncio.run(exercise()) == []
    assert completed_anchor_calls == []
    assert manager.get_delivery_finalization_state(order_id, 1) is None
    _, send_started_at, verification_required = orchestration_send_safety(
        manager,
        order_id,
    )
    assert send_started_at is not None
    assert verification_required == 1


def test_compensation_entry_keeps_unknown_send_for_verification_without_resend(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    order_id = "order-compensation-claim-uncertain"
    card_id = create_binding(manager)
    DeliveryConfigService(manager).save(
        USER_ID,
        card_id,
        ACCOUNT_ID,
        "fixed_link",
        {"url": "https://example.test/compensation-uncertain"},
    )
    manager.insert_or_update_order(
        order_id=order_id,
        item_id=ITEM_ID,
        buyer_id="buyer-1",
        quantity="1",
        order_status="pending_ship",
        cookie_id=ACCOUNT_ID,
    )
    live.delivery_claim_lease_seconds = 1
    live._order_locks = defaultdict(asyncio.Lock)
    live._lock_usage_times = {}
    live.delivery_sent_orders = set()
    live.last_delivery_time = {}
    live.order_status_handler = None
    live.confirmed_orders = {}
    live.order_confirm_cooldown = 300
    renewal_attempted = threading.Event()
    send_calls = []
    delivery_logs = []

    def fail_heartbeat(service, request, claim_token):
        renewal_attempted.set()
        raise RuntimeError("heartbeat database failure")

    async def send_once(*args, **kwargs):
        send_calls.append((args, kwargs))
        await asyncio.to_thread(renewal_attempted.wait)
        await asyncio.Event().wait()

    monkeypatch.setattr(
        DeliveryOrchestrationService,
        "renew_claim",
        fail_heartbeat,
    )
    monkeypatch.setattr(live, "can_auto_delivery", lambda value: True)
    monkeypatch.setattr(live, "is_lock_held", lambda value: False)
    monkeypatch.setattr(live, "is_auto_confirm_enabled", lambda: False)
    monkeypatch.setattr(live, "send_delivery_steps_once", send_once)
    monkeypatch.setattr(live, "_activate_delivery_lock", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        live,
        "_record_delivery_log",
        lambda **kwargs: delivery_logs.append(kwargs),
    )

    first_result = asyncio.run(
        live._send_recovered_delivery_without_sid(
            {"quantity": "1"},
            order_id=order_id,
            item_id=ITEM_ID,
            buyer_id="buyer-1",
            source="test-compensation",
        )
    )

    assert first_result is False
    assert manager.get_delivery_finalization_state(order_id, 1) is None
    assert orchestration_state(manager, order_id)[0] == "sending"
    _, send_started_at, verification_required = orchestration_send_safety(
        manager,
        order_id,
    )
    assert send_started_at is not None
    assert verification_required == 1

    delivery_logs.clear()

    second_result = asyncio.run(
        live._send_recovered_delivery_without_sid(
            {"quantity": "1"},
            order_id=order_id,
            item_id=ITEM_ID,
            buyer_id="buyer-1",
            source="test-compensation-retry",
        )
    )

    assert second_result is False
    assert len(send_calls) == 1
    assert manager.get_delivery_finalization_state(order_id, 1) is None
    assert orchestration_state(manager, order_id)[0] == "sending"
    assert [entry["status"] for entry in delivery_logs] == ["skipped"]
    assert "待人工核实" in delivery_logs[0]["reason"]


@pytest.mark.parametrize(
    "invalid_case",
    [
        "configured_missing",
        "configured_empty",
        "private_missing",
        "token_missing",
        "token_wrong_type",
        "token_stale",
        "card_id_missing",
        "card_id_mismatch",
        "order_scope_mismatch",
        "item_scope_invalid",
        "order_line_missing",
        "quantity_missing",
        "quantity_mismatch",
        "mode_missing",
        "mode_mismatch",
        "idempotency_key_missing",
        "idempotency_key_mismatch",
        "item_scope_mismatch",
        "user_scope_invalid",
        "account_scope_invalid",
    ],
)
def test_configured_send_validates_complete_claim_scope_before_sender(
    runtime,
    invalid_case,
):
    live, manager = runtime
    order_id = f"order-invalid-claim-{invalid_case}"
    delivery_meta = prepare_fixed_link_claim(live, manager, order_id)
    exposed_token = ""
    send_item_id = ITEM_ID

    if invalid_case == "configured_missing":
        delivery_meta.pop("configured")
    elif invalid_case == "configured_empty":
        delivery_meta["configured"] = ""
    elif invalid_case == "private_missing":
        delivery_meta.pop("_orchestration_private")
    elif invalid_case == "token_missing":
        delivery_meta["_orchestration_private"].pop("claim_token")
    elif invalid_case == "token_wrong_type":
        delivery_meta["_orchestration_private"]["claim_token"] = 123
    elif invalid_case == "token_stale":
        exposed_token = "stale-private-claim-token"
        delivery_meta["_orchestration_private"]["claim_token"] = exposed_token
    elif invalid_case == "card_id_missing":
        delivery_meta["card_id"] = None
    elif invalid_case == "card_id_mismatch":
        delivery_meta["card_id"] += 1
    elif invalid_case == "order_scope_mismatch":
        delivery_meta["order_id"] = "different-order"
    elif invalid_case == "item_scope_invalid":
        send_item_id = None
    elif invalid_case == "order_line_missing":
        delivery_meta["order_line_id"] = ""
    elif invalid_case == "quantity_missing":
        delivery_meta["quantity"] = None
    elif invalid_case == "quantity_mismatch":
        delivery_meta["quantity"] = 2
    elif invalid_case == "mode_missing":
        delivery_meta["mode"] = ""
    elif invalid_case == "mode_mismatch":
        delivery_meta["mode"] = "provider_api"
    elif invalid_case == "idempotency_key_missing":
        delivery_meta["idempotency_key"] = ""
    elif invalid_case == "idempotency_key_mismatch":
        delivery_meta["idempotency_key"] = "different-idempotency-key"
    elif invalid_case == "item_scope_mismatch":
        send_item_id = "different-item"
    elif invalid_case == "user_scope_invalid":
        live.user_id = 0
    elif invalid_case == "account_scope_invalid":
        live.cookie_id = ""

    sender_calls = []

    async def sender():
        sender_calls.append(True)
        return "must-not-send"

    with pytest.raises(RuntimeError) as error_info:
        asyncio.run(
            live._send_with_delivery_claim(
                delivery_meta,
                sender,
                order_id=order_id,
                item_id=send_item_id,
            )
        )

    assert sender_calls == []
    if exposed_token:
        assert exposed_token not in str(error_info.value)


def test_only_explicit_configured_false_uses_legacy_sender_without_claim(runtime):
    live, _ = runtime
    sender_calls = []

    async def sender():
        sender_calls.append(True)
        return "legacy-sent"

    result = asyncio.run(
        live._send_with_delivery_claim(
            {"configured": False},
            sender,
            order_id="legacy-order",
            item_id=ITEM_ID,
        )
    )

    assert result == "legacy-sent"
    assert sender_calls == [True]


def test_short_configured_send_begin_send_does_not_block_event_loop(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    order_id = "order-immediate-renew-nonblocking"
    delivery_meta = prepare_fixed_link_claim(live, manager, order_id)
    events = []
    real_begin_send = DeliveryOrchestrationService.begin_send

    def slow_begin_send(service, request, claim_token):
        events.append("begin-start")
        time.sleep(0.12)
        result = real_begin_send(service, request, claim_token)
        events.append("begin-end")
        return result

    async def sender():
        events.append("sender")
        return "sent"

    async def ticker():
        await asyncio.sleep(0.02)
        events.append("tick")

    monkeypatch.setattr(
        DeliveryOrchestrationService,
        "begin_send",
        slow_begin_send,
    )

    async def exercise():
        send_task = asyncio.create_task(
            live._send_with_delivery_claim(
                delivery_meta,
                sender,
                order_id=order_id,
                item_id=ITEM_ID,
            )
        )
        await ticker()
        return await send_task

    assert asyncio.run(exercise()) == "sent"
    assert events.index("begin-start") < events.index("tick") < events.index("begin-end")
    assert events.index("begin-end") < events.index("sender")


@pytest.mark.parametrize("operation", ["mark_sent", "mark_failed"])
def test_configured_terminal_db_calls_do_not_block_event_loop(
    runtime,
    monkeypatch,
    operation,
):
    live, manager = runtime
    order_id = f"order-nonblocking-{operation}"
    delivery_meta = prepare_fixed_link_claim(live, manager, order_id)
    events = []
    real_operation = getattr(DeliveryOrchestrationService, operation)

    def slow_operation(service, request, claim_token, *args, **kwargs):
        events.append("db-start")
        time.sleep(0.12)
        result = real_operation(service, request, claim_token, *args, **kwargs)
        events.append("db-end")
        return result

    async def ticker():
        await asyncio.sleep(0.02)
        events.append("tick")

    monkeypatch.setattr(DeliveryOrchestrationService, operation, slow_operation)

    async def exercise():
        if operation == "mark_sent":
            operation_task = asyncio.create_task(
                live._finalize_delivery_after_send(
                    delivery_meta=delivery_meta,
                    order_id=order_id,
                    item_id=ITEM_ID,
                    skip_confirm=True,
                )
            )
        else:
            operation_task = asyncio.create_task(
                live._mark_configured_delivery_failed(
                    delivery_meta,
                    RuntimeError("sender failed"),
                    order_id=order_id,
                    item_id=ITEM_ID,
                )
            )
        await ticker()
        return await operation_task

    result = asyncio.run(exercise())

    if operation == "mark_sent":
        assert result["success"] is True
        assert orchestration_state(manager, order_id)[0] == "sent"
    else:
        assert result["status"] == "failed"
    assert events.index("db-start") < events.index("tick") < events.index("db-end")


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


def test_main_auto_delivery_verification_required_has_zero_send_finalize_or_notification(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    order_id = "order-main-verification-required"
    live._order_locks = defaultdict(asyncio.Lock)
    live._lock_usage_times = {}
    live.order_status_handler = None
    send_calls = []
    finalize_calls = []
    notifications = []
    delivery_logs = []

    async def return_plan(*args, **kwargs):
        return 1, [{
            "unit_index": 1,
            "quantity": 1,
            "order_line_id": ITEM_ID,
            "binding_snapshot": {},
        }]

    async def verification_result(*args, **kwargs):
        return {
            "success": False,
            "configured": True,
            "content": None,
            "delivery_steps": [],
            "disposition": "verification_required",
            "error": "发货结果待人工核实，已阻止自动重发和确认发货",
        }

    async def record_send(*args, **kwargs):
        send_calls.append((args, kwargs))

    async def record_finalize(*args, **kwargs):
        finalize_calls.append((args, kwargs))
        return {"success": True}

    async def record_notification(*args, **kwargs):
        notifications.append((args, kwargs))

    monkeypatch.setattr(live, "_check_buyer_blacklist_for_action", lambda **kwargs: False)
    monkeypatch.setattr(live, "_extract_order_id", lambda *args: order_id)
    monkeypatch.setattr(live, "_is_trustworthy_buyer_id", lambda value: False)
    monkeypatch.setattr(live, "is_lock_held", lambda value: False)
    monkeypatch.setattr(live, "can_auto_delivery", lambda value: True)
    monkeypatch.setattr(live, "_build_order_auto_delivery_preparation_plan", return_plan)
    monkeypatch.setattr(live, "_auto_delivery", verification_result)
    monkeypatch.setattr(live, "_send_delivery_steps", record_send)
    monkeypatch.setattr(live, "_finalize_delivery_after_send", record_finalize)
    monkeypatch.setattr(live, "send_delivery_failure_notification", record_notification)
    monkeypatch.setattr(
        live,
        "_record_delivery_log",
        lambda **kwargs: delivery_logs.append(kwargs),
    )
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

    assert send_calls == []
    assert finalize_calls == []
    assert notifications == []
    assert [entry["status"] for entry in delivery_logs] == ["skipped"]
    assert "verification_required" in delivery_logs[0]["reason"]
    assert "待人工核实" in delivery_logs[0]["reason"]


def test_simple_auto_delivery_verification_required_has_zero_send_finalize_or_notification(
    runtime,
    monkeypatch,
):
    live, manager = runtime
    create_binding(manager)
    order_id = "order-simple-verification-required"
    live._order_locks = defaultdict(asyncio.Lock)
    live._lock_usage_times = {}
    send_calls = []
    finalize_calls = []
    notifications = []
    delivery_logs = []

    async def owned_item(*args, **kwargs):
        return True

    async def verification_result(*args, **kwargs):
        return {
            "success": False,
            "configured": True,
            "content": None,
            "delivery_steps": [],
            "disposition": "verification_required",
            "error": "发货结果待人工核实，已阻止自动重发和确认发货",
        }

    async def record_send(*args, **kwargs):
        send_calls.append((args, kwargs))

    async def record_finalize(*args, **kwargs):
        finalize_calls.append((args, kwargs))
        return {"success": True}

    async def record_notification(*args, **kwargs):
        notifications.append((args, kwargs))

    monkeypatch.setattr(live, "_ensure_item_owned_by_current_account", owned_item)
    monkeypatch.setattr(live, "_check_buyer_blacklist_for_action", lambda **kwargs: False)
    monkeypatch.setattr(live, "is_lock_held", lambda value: False)
    monkeypatch.setattr(live, "can_auto_delivery", lambda value: True)
    monkeypatch.setattr(live, "_auto_delivery", verification_result)
    monkeypatch.setattr(live, "_send_delivery_steps", record_send)
    monkeypatch.setattr(live, "_finalize_delivery_after_send", record_finalize)
    monkeypatch.setattr(live, "send_delivery_failure_notification", record_notification)
    monkeypatch.setattr(
        live,
        "_record_delivery_log",
        lambda **kwargs: delivery_logs.append(kwargs),
    )
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

    assert send_calls == []
    assert finalize_calls == []
    assert notifications == []
    assert [entry["status"] for entry in delivery_logs] == ["skipped"]
    assert "verification_required" in delivery_logs[0]["reason"]
    assert "待人工核实" in delivery_logs[0]["reason"]


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
    asyncio.run(
        live._mark_configured_delivery_failed(
            first_meta,
            RuntimeError("first sender failed"),
            order_id="order-stale-token",
            item_id=ITEM_ID,
        )
    )
    retried = asyncio.run(live._auto_delivery(**call))

    with pytest.raises(DeliveryOrchestrationError) as stale_error:
        asyncio.run(
            live._mark_configured_delivery_failed(
                first_meta,
                RuntimeError("stale sender failed"),
                order_id="order-stale-token",
                item_id=ITEM_ID,
            )
        )

    assert stale_error.value.code == "claim_token_mismatch"
    assert first["_orchestration_private"]["claim_token"] not in str(stale_error.value)
    state = orchestration_state(manager, "order-stale-token")
    assert state[0] == "sending"
    assert state[1] == retried["_orchestration_private"]["claim_token"]
