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
    second_request = make_request(1, order_line_id="item-7", item_id="item-7")

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


def _request_for_card(request, card_id):
    return request.__class__(**{**request.__dict__, "card_id": card_id})


def _request_with(request, **changes):
    return request.__class__(**{**request.__dict__, **changes})


def test_begin_send_is_one_shot_and_durably_blocks_stale_reclaim(services):
    service, _, _, manager = services
    service.claim_lease_seconds = 1
    request = make_request(
        1,
        order_line_id="line-durable-send-started",
        item_id="item-durable-send-started",
    )
    prepared = service.prepare(request)
    claim_token = prepared["_orchestration_private"]["claim_token"]

    assert service.begin_send(request, claim_token) is True
    assert service.begin_send(request, claim_token) is False

    with manager.lock:
        manager.conn.execute(
            """
            UPDATE delivery_orchestration_states
            SET claimed_at = datetime('now', '-10 minutes')
            WHERE order_line_id = ?
            """,
            ("line-durable-send-started",),
        )
        manager.conn.commit()

    retried = service.prepare_retry(request)
    row = manager.conn.execute(
        """
        SELECT item_id, send_started_at, verification_required, claim_token
        FROM delivery_orchestration_states
        WHERE order_line_id = ?
        """,
        ("line-durable-send-started",),
    ).fetchone()

    assert retried["status"] == "in_progress"
    assert retried.get("claimed") is not True
    assert retried["verification_required"] is True
    assert row[0] == "item-durable-send-started"
    assert row[1] is not None
    assert row[2] == 1
    assert row[3] == claim_token


def test_service_owned_orchestrate_persists_begin_send_before_sender(services):
    service, _, _, manager = services
    request = make_request(
        1,
        order_line_id="line-service-owned-orchestrate",
        item_id="item-service-owned-orchestrate",
    )
    observed = []

    def sender(contents, normalized_request):
        observed.append(
            manager.conn.execute(
                """
                SELECT send_started_at, verification_required
                FROM delivery_orchestration_states
                WHERE order_line_id = ?
                """,
                (normalized_request.order_line_id,),
            ).fetchone()
        )
        return True

    result = service.orchestrate(request, sender)

    assert observed and observed[0][0] is not None
    assert observed[0][1] == 1
    assert result["status"] == "sent"


def test_service_owned_retry_persists_begin_send_before_sender(services):
    service, inventory, _, manager = services
    inventory.import_items(7, 1, "account-a", ["retry-card"])
    request = make_request(
        1,
        order_line_id="line-service-owned-retry",
        item_id="item-service-owned-retry",
        mode="imported_card",
    )
    prepared = service.prepare(request)
    reservation_id = prepared["reservation_id"]
    service.mark_failed(
        request,
        prepared["_orchestration_private"]["claim_token"],
        RuntimeError("initial sender failure"),
    )
    observed = []

    def sender(contents, normalized_request):
        observed.append(
            manager.conn.execute(
                """
                SELECT send_started_at, verification_required, reservation_id
                FROM delivery_orchestration_states
                WHERE order_line_id = ?
                """,
                (normalized_request.order_line_id,),
            ).fetchone()
        )
        return True

    result = service.retry(request, sender)

    assert observed and observed[0][0] is not None
    assert observed[0][1:] == (1, reservation_id)
    assert result["status"] == "sent"
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 1


def test_service_owned_sender_exception_clears_begin_send_for_same_reservation_retry(
    services,
):
    service, inventory, _, manager = services
    inventory.import_items(7, 1, "account-a", ["exception-card"])
    request = make_request(
        1,
        order_line_id="line-service-owned-exception",
        item_id="item-service-owned-exception",
        mode="imported_card",
    )

    failed = service.orchestrate(request, Sender(RuntimeError("sender failed")))
    failed_row = manager.conn.execute(
        """
        SELECT send_started_at, verification_required, reservation_id
        FROM delivery_orchestration_states
        WHERE order_line_id = ?
        """,
        (request.order_line_id,),
    ).fetchone()
    recovered = service.retry(request, Sender())

    assert failed["status"] == "failed"
    assert failed_row == (None, 0, failed["reservation_id"])
    assert recovered["status"] == "sent"
    assert recovered["reservation_id"] == failed["reservation_id"]
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 1


def test_service_owned_keyboard_interrupt_keeps_barrier_past_claim_lease(services):
    service, _, _, manager = services
    service.claim_lease_seconds = 1
    request = make_request(
        1,
        order_line_id="line-service-owned-interrupt",
        item_id="item-service-owned-interrupt",
    )
    interrupted_sender = Sender(KeyboardInterrupt("process interrupted"))

    with pytest.raises(KeyboardInterrupt, match="process interrupted"):
        service.orchestrate(request, interrupted_sender)

    with manager.lock:
        manager.conn.execute(
            """
            UPDATE delivery_orchestration_states
            SET claimed_at = datetime('now', '-10 minutes')
            WHERE order_line_id = ?
            """,
            (request.order_line_id,),
        )
        manager.conn.commit()

    second_sender = Sender()
    retried = service.retry(request, second_sender)
    row = manager.conn.execute(
        """
        SELECT status, send_started_at, verification_required
        FROM delivery_orchestration_states
        WHERE order_line_id = ?
        """,
        (request.order_line_id,),
    ).fetchone()

    assert len(interrupted_sender.calls) == 1
    assert second_sender.calls == []
    assert retried["status"] == "in_progress"
    assert retried["verification_required"] is True
    assert row[0] == "sending"
    assert row[1] is not None
    assert row[2] == 1


@pytest.mark.parametrize(
    "mismatch",
    ["quantity", "mode", "item_id", "idempotency_key", "card_id"],
)
def test_begin_send_rejects_any_persisted_claim_payload_mismatch(
    services,
    mismatch,
):
    service, _, _, manager = services
    request = make_request(
        1,
        order_line_id=f"line-begin-send-mismatch-{mismatch}",
        item_id="item-begin-send-scope",
    )
    prepared = service.prepare(request)
    claim_token = prepared["_orchestration_private"]["claim_token"]
    candidate = request

    if mismatch == "quantity":
        candidate = _request_with(request, quantity=2)
    elif mismatch == "mode":
        candidate = _request_with(
            request,
            delivery_config={"mode": "provider_api"},
        )
    elif mismatch == "item_id":
        candidate = _request_with(request, item_id="different-item")
    elif mismatch == "card_id":
        candidate = _request_for_card(request, 8)
    else:
        with manager.lock:
            manager.conn.execute(
                """
                UPDATE delivery_orchestration_states
                SET idempotency_key = 'persisted-mismatched-key'
                WHERE order_line_id = ?
                """,
                (f"line-begin-send-mismatch-{mismatch}",),
            )
            manager.conn.commit()

    with pytest.raises(DeliveryOrchestrationError) as error_info:
        service.begin_send(candidate, claim_token)

    assert error_info.value.code == "claim_scope_mismatch"
    assert claim_token not in str(error_info.value)
    row = manager.conn.execute(
        """
        SELECT send_started_at, verification_required
        FROM delivery_orchestration_states
        WHERE order_line_id = ?
        """,
        (f"line-begin-send-mismatch-{mismatch}",),
    ).fetchone()
    assert row == (None, 0)


def test_mark_failed_clears_send_barrier_and_retries_same_reservation(services):
    service, inventory, _, manager = services
    inventory.import_items(7, 1, "account-a", ["durable-a", "durable-b"])
    request = make_request(
        2,
        order_line_id="line-begin-send-failed",
        item_id="item-begin-send-failed",
        mode="imported_card",
    )
    prepared = service.prepare(request)
    claim_token = prepared["_orchestration_private"]["claim_token"]

    assert service.begin_send(request, claim_token) is True
    failed = service.mark_failed(request, claim_token, RuntimeError("sender failed"))
    retried = service.prepare_retry(request)

    row = manager.conn.execute(
        """
        SELECT send_started_at, verification_required
        FROM delivery_orchestration_states
        WHERE order_line_id = ?
        """,
        ("line-begin-send-failed",),
    ).fetchone()
    assert failed["status"] == "failed"
    assert row == (None, 0)
    assert retried["status"] == "sending"
    assert retried["reservation_id"] == prepared["reservation_id"]
    assert retried["contents"] == prepared["contents"]


def test_mark_sent_clears_send_barrier_terminally(services):
    service, _, _, manager = services
    request = make_request(
        1,
        order_line_id="line-begin-send-sent",
        item_id="item-begin-send-sent",
    )
    prepared = service.prepare(request)
    claim_token = prepared["_orchestration_private"]["claim_token"]

    assert service.begin_send(request, claim_token) is True
    sent = service.mark_sent(request, claim_token)

    row = manager.conn.execute(
        """
        SELECT status, send_started_at, verification_required
        FROM delivery_orchestration_states
        WHERE order_line_id = ?
        """,
        ("line-begin-send-sent",),
    ).fetchone()
    assert sent["status"] == "sent"
    assert row == ("sent", None, 0)


def test_rebound_card_cannot_create_second_state_for_same_order_line(services):
    service, inventory, _, manager = services
    inventory.import_items(7, 1, "account-a", ["card-7"])
    inventory.import_items(8, 1, "account-a", ["card-8"])
    first_request = make_request(1, order_line_id="line-rebound", mode="imported_card")
    rebound_request = _request_for_card(first_request, 8)

    first = service.prepare(first_request)

    with pytest.raises(DeliveryOrchestrationError) as error:
        service.prepare(rebound_request)

    assert first["status"] == "sending"
    assert error.value.code == "idempotency_conflict"
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM delivery_orchestration_states"
    ).fetchone()[0] == 1
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 1
    assert inventory.get_inventory_summary(8, 1, "account-a")["available"] == 1


def test_concurrent_rebound_cards_have_one_state_claim_and_reservation(monkeypatch, services):
    service, inventory, _, manager = services
    inventory.import_items(7, 1, "account-a", ["card-7"])
    inventory.import_items(8, 1, "account-a", ["card-8"])
    first_request = make_request(1, order_line_id="line-concurrent-rebound", mode="imported_card")
    rebound_request = _request_for_card(first_request, 8)

    original_get_state = service._get_state
    first_reads = set()
    coordination_lock = threading.Lock()
    read_barrier = threading.Barrier(2)

    def coordinated_get_state(request):
        state = original_get_state(request)
        thread_id = threading.get_ident()
        with coordination_lock:
            first_read = thread_id not in first_reads
            if first_read:
                first_reads.add(thread_id)
        if first_read:
            read_barrier.wait(timeout=5)
        return state

    monkeypatch.setattr(service, "_get_state", coordinated_get_state)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(service.prepare, first_request),
            executor.submit(service.prepare, rebound_request),
        ]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=5))
            except DeliveryOrchestrationError as error:
                outcomes.append(error)

    results = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    errors = [outcome for outcome in outcomes if isinstance(outcome, DeliveryOrchestrationError)]
    assert len(results) == 1
    assert len(errors) == 1
    assert errors[0].code == "idempotency_conflict"
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM delivery_orchestration_states"
    ).fetchone()[0] == 1
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
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
    assert prepared["_orchestration_private"]["claim_token"]
    assert "claim_token" not in prepared
    assert "claim_token" not in prepared["meta"]

    sent = service.mark_sent(
        request,
        prepared["_orchestration_private"]["claim_token"],
    )

    assert sent["status"] == "sent"
    assert "claim_token" not in sent
    assert "contents" not in sent


def test_prepare_retry_fresh_request_matches_prepare(services):
    service, _, dispatcher, _ = services
    request = make_request(1, order_line_id="line-fresh-retry")

    prepared = service.prepare_retry(request)

    assert prepared["status"] == "sending"
    assert prepared["claimed"] is True
    assert prepared["contents"] == ["https://example.test/download"]
    assert len(dispatcher.requests) == 1


def test_prepare_retry_returns_sent_without_preparing_again(services):
    service, _, dispatcher, _ = services
    request = make_request(1, order_line_id="line-sent-retry")
    prepared = service.prepare(request)
    service.mark_sent(request, prepared["_orchestration_private"]["claim_token"])

    repeated = service.prepare_retry(request)

    assert repeated["status"] == "sent"
    assert "claimed" not in repeated
    assert "contents" not in repeated
    assert len(dispatcher.requests) == 1


def test_prepare_retry_reclaims_paused_request_when_inventory_is_available(services):
    service, inventory, _, manager = services
    inventory.import_items(7, 1, "account-a", ["a"])
    request = make_request(
        2,
        order_line_id="line-paused-retry",
        mode="imported_card",
    )
    paused = service.prepare(request)
    inventory.import_items(7, 1, "account-a", ["b"])

    prepared = service.prepare_retry(request)

    assert paused["status"] == "paused"
    assert prepared["status"] == "sending"
    assert prepared["claimed"] is True
    assert prepared["contents"] == ["a", "b"]
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 1


def test_mark_sent_is_idempotent_with_exact_terminal_claim_token(services):
    service, _, _, _ = services
    request = make_request(1, order_line_id="line-sent-idempotent")
    prepared = service.prepare(request)
    token = prepared["_orchestration_private"]["claim_token"]

    first = service.mark_sent(request, token)
    repeated = service.mark_sent(request, token)

    assert first["status"] == repeated["status"] == "sent"


def test_mark_sent_terminal_idempotency_requires_exact_claim_token(services):
    service, _, _, _ = services
    request = make_request(1, order_line_id="line-sent-exact-token")
    prepared = service.prepare(request)
    token = prepared["_orchestration_private"]["claim_token"]
    service.mark_sent(request, token)

    with pytest.raises(DeliveryOrchestrationError) as error:
        service.mark_sent(request, "wrong-token")

    assert error.value.code == "claim_token_mismatch"


def test_mark_sent_recovers_when_authoritative_sent_state_retains_token(services):
    service, _, _, manager = services
    request = make_request(1, order_line_id="line-sent-retained-token")
    prepared = service.prepare(request)
    token = prepared["_orchestration_private"]["claim_token"]
    manager.conn.execute(
        """
        UPDATE delivery_orchestration_states
        SET status = 'sent'
        WHERE order_line_id = ?
        """,
        ("line-sent-retained-token",),
    )
    manager.conn.commit()

    recovered = service.mark_sent(request, token)

    assert recovered["status"] == "sent"


def test_mark_sent_rereads_authoritative_state_after_lost_transition(
    monkeypatch,
    services,
):
    service, _, _, manager = services
    request = make_request(1, order_line_id="line-sent-transition-race")
    prepared = service.prepare(request)
    token = prepared["_orchestration_private"]["claim_token"]

    def lose_to_concurrent_sent(*args, **kwargs):
        manager.conn.execute(
            """
            UPDATE delivery_orchestration_states
            SET status = 'sent'
            WHERE order_line_id = ?
            """,
            ("line-sent-transition-race",),
        )
        manager.conn.commit()
        return False

    monkeypatch.setattr(service, "_transition_claim", lose_to_concurrent_sent)

    recovered = service.mark_sent(request, token)

    assert recovered["status"] == "sent"


def test_mark_failed_is_idempotent_and_preserves_reservation(services):
    service, inventory, _, manager = services
    inventory.import_items(7, 1, "account-a", ["a"])
    request = make_request(
        1,
        order_line_id="line-failed-idempotent",
        mode="imported_card",
    )
    prepared = service.prepare(request)
    token = prepared["_orchestration_private"]["claim_token"]

    first = service.mark_failed(request, token, RuntimeError("send failed"))
    repeated = service.mark_failed(request, token, RuntimeError("send failed again"))

    assert first["status"] == repeated["status"] == "failed"
    assert repeated["reservation_id"] == prepared["reservation_id"]
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 1


def test_reclaimed_failure_rejects_previous_terminal_claim_token(services):
    service, _, _, _ = services
    request = make_request(1, order_line_id="line-failed-stale-terminal-token")
    first = service.prepare(request)
    stale_token = first["_orchestration_private"]["claim_token"]
    service.mark_failed(request, stale_token, RuntimeError("first failure"))

    retried = service.prepare_retry(request)
    current_token = retried["_orchestration_private"]["claim_token"]
    service.mark_failed(request, current_token, RuntimeError("second failure"))

    with pytest.raises(DeliveryOrchestrationError) as error:
        service.mark_failed(request, stale_token, RuntimeError("late failure"))

    assert current_token != stale_token
    assert error.value.code == "claim_token_mismatch"


def test_mark_failed_rereads_authoritative_state_after_lost_transition(
    monkeypatch,
    services,
):
    service, _, _, manager = services
    request = make_request(1, order_line_id="line-failed-transition-race")
    prepared = service.prepare(request)
    token = prepared["_orchestration_private"]["claim_token"]

    def lose_to_concurrent_failure(*args, **kwargs):
        manager.conn.execute(
            """
            UPDATE delivery_orchestration_states
            SET status = 'failed'
            WHERE order_line_id = ?
            """,
            ("line-failed-transition-race",),
        )
        manager.conn.commit()
        return False

    monkeypatch.setattr(service, "_transition_claim", lose_to_concurrent_failure)

    recovered = service.mark_failed(request, token, RuntimeError("send failed"))

    assert recovered["status"] == "failed"


def test_renew_claim_only_renews_current_sending_token(services):
    service, _, _, manager = services
    service.claim_lease_seconds = 1
    request = make_request(1, order_line_id="line-renew-claim")
    prepared = service.prepare(request)
    token = prepared["_orchestration_private"]["claim_token"]
    manager.conn.execute(
        """
        UPDATE delivery_orchestration_states
        SET claimed_at = datetime('now', '-10 minutes')
        WHERE order_line_id = ?
        """,
        ("line-renew-claim",),
    )
    manager.conn.commit()

    assert service.renew_claim(request, "wrong-token") is False
    assert service.renew_claim(request, token) is True
    renewed_at = manager.conn.execute(
        """
        SELECT claimed_at FROM delivery_orchestration_states
        WHERE order_line_id = ?
        """,
        ("line-renew-claim",),
    ).fetchone()[0]
    service.mark_failed(request, token, RuntimeError("send failed"))

    assert renewed_at > "2000-01-01"
    assert service.renew_claim(request, token) is False


def test_external_prepare_can_mark_failed_with_current_claim(services):
    service, _, _, _ = services
    request = make_request(1, order_line_id="line-failed")
    prepared = service.prepare(request)

    failed = service.mark_failed(
        request,
        prepared["_orchestration_private"]["claim_token"],
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
    assert service.mark_sent(
        request,
        prepared["_orchestration_private"]["claim_token"],
    )["status"] == "sent"


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


def test_prepare_retry_reclaims_stale_claim_without_allowing_old_token(services):
    service, inventory, _, manager = services
    service.claim_lease_seconds = 1
    inventory.import_items(7, 1, "account-a", ["a", "b"])
    request = make_request(
        2,
        order_line_id="line-stale-prepare-retry",
        mode="imported_card",
    )
    prepared = service.prepare(request)
    stale_token = prepared["_orchestration_private"]["claim_token"]
    manager.conn.execute(
        """
        UPDATE delivery_orchestration_states
        SET claimed_at = datetime('now', '-10 minutes')
        WHERE order_line_id = ?
        """,
        ("line-stale-prepare-retry",),
    )
    manager.conn.commit()

    reclaimed = service.prepare_retry(request)
    current_token = reclaimed["_orchestration_private"]["claim_token"]

    assert reclaimed["status"] == "sending"
    assert current_token != stale_token
    assert reclaimed["reservation_id"] == prepared["reservation_id"]
    assert reclaimed["contents"] == prepared["contents"]
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 1
    assert inventory.get_inventory_summary(7, 1, "account-a")["sent"] == 2
    with pytest.raises(DeliveryOrchestrationError) as sent_error:
        service.mark_sent(request, stale_token)
    with pytest.raises(DeliveryOrchestrationError) as failed_error:
        service.mark_failed(request, stale_token, RuntimeError("late failure"))
    assert sent_error.value.code == "claim_token_mismatch"
    assert failed_error.value.code == "claim_token_mismatch"
    assert service.mark_sent(request, current_token)["status"] == "sent"


def test_expired_claim_token_cannot_overwrite_reclaimed_sender(services):
    service, _, _, manager = services
    request = make_request(1, order_line_id="line-reclaimed")
    prepared = service.prepare(request)
    stale_token = prepared["_orchestration_private"]["claim_token"]
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


def test_active_prepare_renews_claim_lease_and_cannot_be_reclaimed(
    monkeypatch,
    services,
):
    service, _, dispatcher, _ = services
    service.claim_lease_seconds = 1
    request = make_request(1, order_line_id="line-prepare-heartbeat")
    prepare_started = threading.Event()
    release_prepare = threading.Event()
    original_prepare = dispatcher.prepare
    prepare_calls = []

    def slow_first_prepare(delivery_request):
        prepare_calls.append(delivery_request)
        if len(prepare_calls) == 1:
            prepare_started.set()
            assert release_prepare.wait(5)
        return original_prepare(delivery_request)

    monkeypatch.setattr(dispatcher, "prepare", slow_first_prepare)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future = executor.submit(service.prepare, request)
        assert prepare_started.wait(5)
        time.sleep(1.25)

        second = service.prepare_retry(request)

        release_prepare.set()
        first = first_future.result(timeout=5)

    assert second["status"] == "in_progress"
    assert second["state"] == "sending"
    assert "claimed" not in second
    assert first["status"] == "sending"
    assert len(prepare_calls) == 1
    assert not any(
        thread.name.startswith("delivery-claim-heartbeat-")
        for thread in threading.enumerate()
    )


def test_prepare_exception_stops_claim_heartbeat(monkeypatch, services):
    service, _, dispatcher, _ = services
    service.claim_lease_seconds = 1
    request = make_request(1, order_line_id="line-prepare-heartbeat-error")
    prepare_started = threading.Event()
    release_prepare = threading.Event()

    def failing_prepare(delivery_request):
        prepare_started.set()
        assert release_prepare.wait(5)
        raise RuntimeError("prepare failed")

    monkeypatch.setattr(dispatcher, "prepare", failing_prepare)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.prepare, request)
        assert prepare_started.wait(5)
        assert any(
            thread.name.startswith("delivery-claim-heartbeat-")
            for thread in threading.enumerate()
        )
        release_prepare.set()
        result = future.result(timeout=5)

    assert result["status"] == "failed"
    assert not any(
        thread.name.startswith("delivery-claim-heartbeat-")
        for thread in threading.enumerate()
    )
