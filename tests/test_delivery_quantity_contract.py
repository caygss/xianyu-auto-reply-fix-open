import pytest

from card_inventory_service import CardInventoryService
from db_manager import DBManager
from delivery_orchestration_service import (
    DeliveryOrchestrationError,
    DeliveryOrchestrationRequest,
    DeliveryOrchestrationService,
)


class FakeDispatcher:
    def __init__(self, inventory):
        self.inventory = inventory
        self.requests = []

    def prepare(self, request):
        self.requests.append(request)
        mode = request.mode or request.delivery_config["mode"]
        if mode in {"imported_card", "generated_card"}:
            committed = self.inventory.commit_reservation(
                request.reservation_id,
                request.user_id,
                request.card_id,
                request.account_id,
            )
            return {
                "mode": mode,
                "content": "\n".join(committed["items"]),
                "content_type": "text",
            }
        if mode == "fixed_link":
            return {
                "mode": mode,
                "content": request.delivery_config["url"],
                "content_type": "text",
            }
        return {"mode": mode, "content": "provider-content", "content_type": "text"}


class Sender:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, contents, request):
        self.calls.append({"contents": list(contents), "request": request})
        if self.error:
            raise self.error
        return True


@pytest.fixture
def services(tmp_path):
    manager = DBManager(str(tmp_path / "delivery.sqlite3"))
    inventory = CardInventoryService(manager)
    dispatcher = FakeDispatcher(inventory)
    service = DeliveryOrchestrationService(manager, inventory, dispatcher)
    yield service, inventory, dispatcher, manager
    manager.close()


def make_request(quantity=None, *, order_line_id=None, item_id="item-7", mode="fixed_link"):
    config = {"mode": mode}
    if mode == "fixed_link":
        config["url"] = "https://example.test/download"
    return DeliveryOrchestrationRequest(
        user_id=1,
        card_id=7,
        account_id="account-a",
        order_id="order-1",
        order_line_id=order_line_id,
        item_id=item_id,
        quantity=quantity,
        delivery_config=config,
    )


def test_missing_quantity_defaults_to_one_and_order_line_falls_back_to_item(services):
    service, _, _, _ = services

    result = service.prepare(make_request())

    assert result["quantity"] == 1
    assert result["order_line_id"] == "item-7"
    assert result["status"] == "sending"


@pytest.mark.parametrize("raw", [0, -1, "abc", "1.5", True, 101])
def test_invalid_quantity_is_rejected_without_state(services, raw):
    service, _, _, manager = services

    with pytest.raises(DeliveryOrchestrationError) as error:
        service.prepare(make_request(raw))

    assert error.value.code == "invalid_quantity"
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM delivery_orchestration_states"
    ).fetchone()[0] == 0


def test_insufficient_card_inventory_pauses_whole_order_without_partial_reservation(services):
    service, inventory, _, manager = services
    inventory.import_items(7, 1, "account-a", ["a", "b"])
    sender = Sender()
    request = make_request(3, mode="imported_card")

    result = service.orchestrate(request, sender)

    assert result["status"] == "paused"
    assert result["error_code"] == "insufficient_inventory"
    assert result["meta"]["shortage"] == 1
    assert inventory.get_inventory_summary(7, 1, "account-a")["available"] == 2
    assert sender.calls == []
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 0


def test_quantity_three_commits_three_distinct_cards_and_sends_once(services):
    service, inventory, dispatcher, manager = services
    inventory.import_items(7, 1, "account-a", ["a", "b", "c"])
    sender = Sender()

    result = service.orchestrate(make_request("3", mode="imported_card"), sender)

    assert result["status"] == "sent"
    assert len(sender.calls) == 1
    assert len(sender.calls[0]["contents"]) == 3
    assert len(set(sender.calls[0]["contents"])) == 3
    assert len(dispatcher.requests) == 1
    assert inventory.get_inventory_summary(7, 1, "account-a")["sent"] == 3
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 1


def test_fixed_link_quantity_still_sends_one_content(services):
    service, _, _, _ = services
    sender = Sender()

    result = service.orchestrate(make_request(3), sender)

    assert result["status"] == "sent"
    assert sender.calls[0]["contents"] == ["https://example.test/download"]


def test_send_failure_is_recoverable_without_re_reserving_cards(services):
    service, inventory, _, manager = services
    inventory.import_items(7, 1, "account-a", ["a", "b"])
    request = make_request(2, mode="imported_card")

    failed = service.orchestrate(request, Sender(RuntimeError("send failed")))
    recovered_sender = Sender()
    recovered = service.retry(request, recovered_sender)

    assert failed["status"] == "failed"
    assert recovered["status"] == "sent"
    assert len(recovered_sender.calls[0]["contents"]) == 2
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 1
    assert inventory.get_inventory_summary(7, 1, "account-a")["sent"] == 2


def test_prepare_failure_keeps_reservation_and_retry_reuses_it(services):
    service, inventory, _, manager = services
    inventory.import_items(7, 1, "account-a", ["a"])
    request = make_request(1, mode="imported_card")

    class FailingDispatcher:
        def prepare(self, delivery_request):
            raise RuntimeError("dispatcher unavailable")

    service.dispatcher = FailingDispatcher()
    failed = service.prepare(request)

    assert failed["status"] == "failed"
    assert failed["reservation_id"]
    assert inventory.get_inventory_summary(7, 1, "account-a")["available"] == 0

    service.dispatcher = FakeDispatcher(inventory)
    recovered = service.retry(request, Sender())

    assert recovered["status"] == "sent"
    assert recovered["reservation_id"]
    assert inventory.get_inventory_summary(7, 1, "account-a")["sent"] == 1
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 1


def test_prepare_retry_reuses_failed_reservation_and_allocated_cards(services):
    service, inventory, dispatcher, manager = services
    inventory.import_items(7, 1, "account-a", ["a", "b"])
    request = make_request(
        2,
        order_line_id="line-prepare-retry",
        mode="imported_card",
    )
    prepared = service.prepare(request)
    claim_token = prepared["_orchestration_private"]["claim_token"]
    failed = service.mark_failed(request, claim_token, RuntimeError("send failed"))

    retried = service.prepare_retry(request)

    assert failed["status"] == "failed"
    assert retried["status"] == "sending"
    assert retried["claimed"] is True
    assert retried["contents"] == prepared["contents"]
    assert retried["reservation_id"] == prepared["reservation_id"]
    assert len(dispatcher.requests) == 2
    assert manager.conn.execute(
        "SELECT COUNT(*) FROM card_inventory_reservations"
    ).fetchone()[0] == 1
    assert inventory.get_inventory_summary(7, 1, "account-a")["sent"] == 2
