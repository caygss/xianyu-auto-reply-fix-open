from test_delivery_quantity_contract import Sender, make_request, services


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
    assert first["status"] == second["status"] == "sending"
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
