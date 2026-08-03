from test_delivery_quantity_contract import Sender, make_request, services

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import pytest

from delivery_orchestration_service import DeliveryOrchestrationError


def test_duplicate_callback_returns_existing_result_without_sending_again(services):
    service, inventory, dispatcher, manager = services
    inventory.import_items(7, 1, "account-a", ["a"])
    sender = Sender()
    request = make_request(1, order_line_id="line-1", mode="imported_card")

    first = service.orchestrate(request, sender)
    repeated = service.orchestrate(request, Sender())

    assert first["status"] == repeated["status"] == "sent"
    assert len(sender.calls) == 1
    assert len(dispatcher.requests) == 1
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 1


def test_order_line_fallback_matches_item_id_for_idempotency(services):
    service, _, dispatcher, manager = services
    first_request = make_request(1, order_line_id=None, item_id="item-7")
    second_request = make_request(1, order_line_id="item-7", item_id="different-item")

    first = service.prepare(first_request)
    second = service.prepare(second_request)

    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["status"] == "sending"
    assert second["status"] == "in_progress"
    assert second["state"] == "sending"
    assert len(dispatcher.requests) == 1
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM delivery_orchestration_states"
    ).fetchone()[0] == 1


def test_same_order_different_line_has_independent_idempotency_scope(services):
    service, _, dispatcher, manager = services

    first = service.prepare(make_request(1, order_line_id="line-1"))
    second = service.prepare(make_request(1, order_line_id="line-2"))

    assert first["idempotency_key"] != second["idempotency_key"]
    assert len(dispatcher.requests) == 2
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM delivery_orchestration_states"
    ).fetchone()[0] == 2


def test_provider_request_carries_quantity_and_idempotency_key(services):
    service, _, dispatcher, _ = services
    request = make_request(3, order_line_id="line-1", mode="provider_api")

    result = service.prepare(request)

    captured = dispatcher.requests[0]
    assert result["status"] == "sending"
    assert captured.quantity == 3
    assert captured.idempotency_key == result["idempotency_key"]


def test_concurrent_duplicate_callbacks_only_claim_one_sender(services):
    service, _, dispatcher, _ = services
    sender_started = threading.Event()
    release_sender = threading.Event()
    calls = []

    def blocking_sender(contents, request):
        calls.append(list(contents))
        sender_started.set()
        assert release_sender.wait(5)
        return True

    request = make_request(1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(service.orchestrate, request, blocking_sender)
        assert sender_started.wait(5)
        second = service.orchestrate(request, Sender())
        release_sender.set()
        first = first_future.result(timeout=5)

    assert first["status"] == "sent"
    assert second["status"] == "in_progress"
    assert second["state"] == "sending"
    assert len(calls) == 1
    assert len(dispatcher.requests) == 1


def test_supplied_idempotency_key_must_match_internal_scope(services):
    service, _, _, manager = services
    request = make_request(1)
    request = request.__class__(**{**request.__dict__, "idempotency_key": "external-key"})

    with pytest.raises(DeliveryOrchestrationError) as error:
        service.prepare(request)

    assert error.value.code == "idempotency_key_mismatch"
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM delivery_orchestration_states"
    ).fetchone()[0] == 0


def test_same_order_different_lines_reserve_and_send_distinct_cards(services):
    service, inventory, _, manager = services
    inventory.import_items(7, 1, "account-a", ["a", "b"])

    first = service.orchestrate(
        make_request(1, order_line_id="line-1", item_id="item-7", mode="imported_card"),
        Sender(),
    )
    second_sender = Sender()
    second = service.orchestrate(
        make_request(1, order_line_id="line-2", item_id="item-7", mode="imported_card"),
        second_sender,
    )

    assert first["status"] == second["status"] == "sent"
    assert second_sender.calls[0]["contents"] != ["a"]
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 2
    assert inventory.get_inventory_summary(7, 1, "account-a")["sent"] == 2


def test_concurrent_retries_only_one_thread_claims_sender(monkeypatch, services):
    service, inventory, _, manager = services
    inventory.import_items(7, 1, "account-a", ["a", "b"])
    request = make_request(2, order_line_id="line-1", mode="imported_card")
    failed = service.orchestrate(request, Sender(RuntimeError("send failed")))
    reservation_id = failed["reservation_id"]

    original_get_state = service._get_state
    original_update_state = service._update_state
    original_claim_sending = service._claim_sending
    initial_read_barrier = threading.Barrier(2)
    initial_read_threads = set()
    coordination_lock = threading.Lock()
    first_claimed = threading.Event()
    second_claimed = threading.Event()
    pending_updates = 0
    retry_claims = 0

    def coordinated_get_state(normalized_request):
        state = original_get_state(normalized_request)
        thread_id = threading.get_ident()
        should_wait = False
        with coordination_lock:
            if state and state["status"] == "failed" and thread_id not in initial_read_threads:
                initial_read_threads.add(thread_id)
                should_wait = True
        if should_wait:
            initial_read_barrier.wait(timeout=5)
        return state

    def coordinated_update_state(state_id, *, status, **kwargs):
        nonlocal pending_updates
        if status == "pending":
            with coordination_lock:
                pending_updates += 1
                update_number = pending_updates
            if update_number == 2:
                assert first_claimed.wait(5)
        return original_update_state(state_id, status=status, **kwargs)

    def coordinated_claim_sending(state_id, allowed_statuses, **kwargs):
        nonlocal retry_claims
        claimed = original_claim_sending(state_id, allowed_statuses, **kwargs)
        claim_number = None
        with coordination_lock:
            if claimed and pending_updates:
                retry_claims += 1
                claim_number = retry_claims
        if claim_number == 1:
            first_claimed.set()
            assert second_claimed.wait(5)
        elif claim_number == 2:
            second_claimed.set()
        return claimed

    monkeypatch.setattr(service, "_get_state", coordinated_get_state)
    monkeypatch.setattr(service, "_update_state", coordinated_update_state)
    monkeypatch.setattr(service, "_claim_sending", coordinated_claim_sending)

    sender_started = threading.Event()
    release_sender = threading.Event()
    sender_calls = []

    def blocking_sender(contents, normalized_request):
        sender_calls.append(list(contents))
        sender_started.set()
        assert release_sender.wait(5)
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(service.retry, request, blocking_sender),
            executor.submit(service.retry, request, blocking_sender),
        ]
        assert sender_started.wait(5)
        completed, _ = wait(futures, timeout=2, return_when=FIRST_COMPLETED)
        release_sender.set()
        results = [future.result(timeout=5) for future in futures]

    assert completed
    assert sorted(result["status"] for result in results) == ["in_progress", "sent"]
    assert len(sender_calls) == 1
    assert all(len(contents) == 2 for contents in sender_calls)
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 1
    assert {result["reservation_id"] for result in results} == {reservation_id}
    assert inventory.get_inventory_summary(7, 1, "account-a")["sent"] == 2


def test_external_prepare_can_mark_sent_with_current_claim(services):
    service, _, _, _ = services
    request = make_request(1, order_line_id="line-sent")

    prepared = service.prepare(request)

    assert prepared["status"] == "sending"
    assert prepared["claim_token"]
    assert "claim_token" not in prepared["meta"]

    sent = service.mark_sent(request, prepared["claim_token"])

    assert sent["status"] == "sent"
    assert "claim_token" not in sent
    assert "contents" not in sent


def test_external_prepare_can_mark_failed_with_current_claim(services):
    service, _, _, _ = services
    request = make_request(1, order_line_id="line-failed")
    prepared = service.prepare(request)

    failed = service.mark_failed(
        request,
        prepared["claim_token"],
        DeliveryOrchestrationError("send_failed", "交付消息发送失败", "sender"),
    )

    assert failed["status"] == "failed"
    assert failed["error_code"] == "send_failed"
    assert "claim_token" not in failed


def test_wrong_claim_token_cannot_mark_terminal_state(services):
    service, _, _, manager = services
    request = make_request(1, order_line_id="line-token")
    prepared = service.prepare(request)

    with pytest.raises(DeliveryOrchestrationError) as sent_error:
        service.mark_sent(request, "wrong-token")
    with pytest.raises(DeliveryOrchestrationError) as failed_error:
        service.mark_failed(request, "wrong-token", RuntimeError("send failed"))

    assert sent_error.value.code == "claim_token_mismatch"
    assert failed_error.value.code == "claim_token_mismatch"
    assert manager.conn.execute(
        "SELECT status FROM delivery_orchestration_states WHERE order_line_id = ?",
        ("line-token",),
    ).fetchone()[0] == "sending"
    assert service.mark_sent(request, prepared["claim_token"])["status"] == "sent"


def test_retry_reclaims_stale_sending_and_reuses_reservation(services):
    service, inventory, _, manager = services
    inventory.import_items(7, 1, "account-a", ["a", "b"])
    request = make_request(2, order_line_id="line-stale", mode="imported_card")
    prepared = service.prepare(request)
    reservation_id = prepared["reservation_id"]
    active_sender = Sender()

    active = service.retry(request, active_sender)

    assert active["status"] == "in_progress"
    assert active_sender.calls == []

    manager.conn.execute(
        """
        UPDATE delivery_orchestration_states
        SET claimed_at = datetime('now', '-10 minutes')
        WHERE order_line_id = ?
        """,
        ("line-stale",),
    )
    manager.conn.commit()
    recovered_sender = Sender()

    recovered = service.retry(request, recovered_sender)

    assert recovered["status"] == "sent"
    assert len(recovered_sender.calls) == 1
    assert len(recovered_sender.calls[0]["contents"]) == 2
    assert recovered["reservation_id"] == reservation_id
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 1
    assert inventory.get_inventory_summary(7, 1, "account-a")["sent"] == 2


def test_expired_claim_token_cannot_overwrite_reclaimed_sender(services):
    service, _, _, manager = services
    request = make_request(1, order_line_id="line-reclaimed")
    prepared = service.prepare(request)
    stale_token = prepared["claim_token"]
    manager.conn.execute(
        """
        UPDATE delivery_orchestration_states
        SET claimed_at = datetime('now', '-10 minutes')
        WHERE order_line_id = ?
        """,
        ("line-reclaimed",),
    )
    manager.conn.commit()
    sender_started = threading.Event()
    release_sender = threading.Event()

    def blocking_sender(contents, normalized_request):
        sender_started.set()
        assert release_sender.wait(5)
        return True

    with ThreadPoolExecutor(max_workers=1) as executor:
        recovered_future = executor.submit(service.retry, request, blocking_sender)
        assert sender_started.wait(5)
        with pytest.raises(DeliveryOrchestrationError) as error:
            service.mark_failed(request, stale_token, RuntimeError("late failure"))
        current = manager.conn.execute(
            """
            SELECT status, claim_token FROM delivery_orchestration_states
            WHERE order_line_id = ?
            """,
            ("line-reclaimed",),
        ).fetchone()
        release_sender.set()
        recovered = recovered_future.result(timeout=5)

    assert error.value.code == "claim_token_mismatch"
    assert current[0] == "sending"
    assert current[1] != stale_token
    assert recovered["status"] == "sent"


def test_active_sender_renews_claim_lease_and_cannot_be_reclaimed(services):
    service, _, _, _ = services
    service.claim_lease_seconds = 1
    request = make_request(1, order_line_id="line-heartbeat")
    sender_started = threading.Event()
    release_sender = threading.Event()

    def slow_sender(contents, normalized_request):
        sender_started.set()
        assert release_sender.wait(5)
        return True

    second_sender = Sender()
    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future = executor.submit(service.orchestrate, request, slow_sender)
        assert sender_started.wait(5)
        time.sleep(1.25)

        second = service.retry(request, second_sender)

        release_sender.set()
        first = first_future.result(timeout=5)

    assert second["status"] == "in_progress"
    assert second_sender.calls == []
    assert first["status"] == "sent"
