import pytest
from io import StringIO
from concurrent.futures import ThreadPoolExecutor
from loguru import logger

from card_inventory_service import CardInventoryError, CardInventoryService
from db_manager import DBManager


@pytest.fixture
def inventory(tmp_path):
    manager = DBManager(str(tmp_path / "inventory.sqlite3"))
    yield CardInventoryService(manager), manager
    manager.close()


def test_save_settings_persists_per_account_scope(inventory):
    service, manager = inventory

    result = service.save_settings(
        card_id=7,
        user_id=1,
        account_id="cookie-a",
        stock_ceiling=3,
        low_stock_threshold=1,
        auto_replenish=True,
    )

    assert result["stock_ceiling"] == 3
    assert service.get_inventory_summary(7, 1, "cookie-a")["stock_ceiling"] == 3
    assert service.get_inventory_summary(7, 1, "cookie-b")["stock_ceiling"] == 100


def test_settings_reject_non_positive_ceiling(inventory):
    service, _ = inventory

    with pytest.raises(CardInventoryError, match="库存上限"):
        service.save_settings(7, 1, "cookie-a", stock_ceiling=0)


def test_lowering_ceiling_clamps_implicit_default_threshold(inventory):
    service, _ = inventory

    result = service.save_settings(7, 1, "cookie-a", stock_ceiling=3)

    assert result["stock_ceiling"] == 3
    assert result["low_stock_threshold"] == 3


def test_import_deduplicates_blank_lines_and_encrypts_without_logging_secret(inventory):
    service, manager = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=3)
    sink = StringIO()
    sink_id = logger.add(sink, format="{message}", level="INFO")
    try:
        result = service.import_items(
            7, 1, "cookie-a", [" secret-a ", "", "secret-a", "secret-b"]
        )
    finally:
        logger.remove(sink_id)

    assert result["inserted"] == 2
    assert result["duplicates"] == 1
    assert result["blank"] == 1
    assert service.get_inventory_summary(7, 1, "cookie-a")["available"] == 2
    with manager.lock:
        stored = manager.conn.execute(
            "SELECT secret_text FROM card_inventory_items"
        ).fetchone()[0]
    assert stored != "secret-a"
    assert "secret-a" not in sink.getvalue()


def test_import_over_ceiling_rejects_entire_batch(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=2)
    service.import_items(7, 1, "cookie-a", ["secret-a"])

    with pytest.raises(CardInventoryError, match="库存上限") as error:
        service.import_items(7, 1, "cookie-a", ["secret-b", "secret-c"])

    assert error.value.code == "inventory_ceiling_exceeded"
    assert service.get_inventory_summary(7, 1, "cookie-a")["available"] == 1


def test_generate_fills_gap_with_unique_opaque_values(inventory):
    service, manager = inventory
    service.save_settings(
        7,
        1,
        "cookie-a",
        stock_ceiling=8,
        generator_prefix="AC-",
        generator_length=20,
    )
    service.import_items(7, 1, "cookie-a", ["manual-secret"])

    result = service.generate_items(7, 1, "cookie-a")

    assert result["generated"] == 7
    assert service.get_inventory_summary(7, 1, "cookie-a")["available"] == 8
    with manager.lock:
        rows = manager.conn.execute(
            "SELECT secret_text FROM card_inventory_items WHERE card_id = 7"
        ).fetchall()
    values = [manager._decrypt_secret(row[0]) for row in rows]
    generated_values = [value for value in values if value != "manual-secret"]
    assert len(generated_values) == len(set(generated_values)) == 7
    assert all(value.startswith("AC-") for value in generated_values)
    assert all(len(value) >= 20 for value in generated_values)
    assert all(
        all(character not in value for character in "IO01")
        for value in generated_values
    )


def test_generate_does_not_count_sent_items_toward_ceiling(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=2)
    service.import_items(7, 1, "cookie-a", ["secret-a", "secret-b"])
    reservation = service.reserve_items(7, 1, "cookie-a", "order-1", 1)
    service.commit_reservation(reservation["reservation_id"], 1, 7, "cookie-a")

    assert service.generate_items(7, 1, "cookie-a")["generated"] == 1
    assert service.get_inventory_summary(7, 1, "cookie-a")["sent"] == 1


def test_reserve_requires_positive_quantity_and_never_partially_reserves(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=5)
    service.import_items(7, 1, "cookie-a", ["a", "b"])

    with pytest.raises(CardInventoryError, match="购买数量"):
        service.reserve_items(7, 1, "cookie-a", "order-0", 0)
    with pytest.raises(CardInventoryError, match="库存不足") as error:
        service.reserve_items(7, 1, "cookie-a", "order-1", 3)

    assert error.value.code == "insufficient_inventory"
    assert service.get_inventory_summary(7, 1, "cookie-a")["available"] == 2


def test_reserve_commit_returns_n_distinct_values_and_decrements_available(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=5)
    service.import_items(7, 1, "cookie-a", ["a", "b", "c"])

    reservation = service.reserve_items(7, 1, "cookie-a", "order-1", 2)
    assert reservation["quantity"] == 2
    assert service.get_inventory_summary(7, 1, "cookie-a")["reserved"] == 2
    committed = service.commit_reservation(reservation["reservation_id"], 1, 7, "cookie-a")

    assert committed["status"] == "committed"
    assert len(committed["items"]) == 2
    assert len(set(committed["items"])) == 2
    assert service.get_inventory_summary(7, 1, "cookie-a")["sent"] == 2


def test_release_is_idempotent_and_scope_mismatch_cannot_commit(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=3)
    service.import_items(7, 1, "cookie-a", ["a", "b"])
    reservation = service.reserve_items(7, 1, "cookie-a", "order-1", 2)

    released = service.release_reservation(reservation["reservation_id"], 1, 7, "cookie-a")
    repeated = service.release_reservation(reservation["reservation_id"], 1, 7, "cookie-a")
    assert released["status"] == repeated["status"] == "released"
    assert service.get_inventory_summary(7, 1, "cookie-a")["available"] == 2

    with pytest.raises(CardInventoryError) as error:
        service.commit_reservation(reservation["reservation_id"], 1, 7, "cookie-b")
    assert error.value.code == "scope_mismatch"


def test_duplicate_order_reuses_reservation_and_commit_result(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=3)
    service.import_items(7, 1, "cookie-a", ["a", "b"])

    first = service.reserve_items(7, 1, "cookie-a", "order-1", 1)
    second = service.reserve_items(7, 1, "cookie-a", "order-1", 1)
    assert second["reservation_id"] == first["reservation_id"]
    assert service.get_inventory_summary(7, 1, "cookie-a")["reserved"] == 1

    committed = service.commit_reservation(first["reservation_id"], 1, 7, "cookie-a")
    repeated = service.commit_reservation(first["reservation_id"], 1, 7, "cookie-a")
    assert repeated == committed


def test_duplicate_callback_with_idempotency_key_reuses_reservation(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=3)
    service.import_items(7, 1, "cookie-a", ["a", "b"])

    first = service.reserve_items(
        7, 1, "cookie-a", "order-1", 1, idempotency_key="callback-1"
    )
    repeated = service.reserve_items(
        7, 1, "cookie-a", "order-1", 1, idempotency_key="callback-1"
    )

    assert repeated == first
    assert service.get_inventory_summary(7, 1, "cookie-a")["reserved"] == 1


def test_commit_rejects_wrong_card_scope(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=2)
    service.save_settings(8, 1, "cookie-a", stock_ceiling=2)
    service.import_items(7, 1, "cookie-a", ["product-7-secret"])
    reservation = service.reserve_items(7, 1, "cookie-a", "order-1", 1)

    with pytest.raises(CardInventoryError, match="商品") as error:
        service.commit_reservation(reservation["reservation_id"], 1, 8, "cookie-a")

    assert error.value.code == "scope_mismatch"
    assert service.get_inventory_summary(7, 1, "cookie-a")["reserved"] == 1
    assert service.get_inventory_summary(7, 1, "cookie-a")["sent"] == 0


def test_release_rejects_wrong_card_scope(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=2)
    service.save_settings(8, 1, "cookie-a", stock_ceiling=2)
    service.import_items(7, 1, "cookie-a", ["product-7-secret"])
    reservation = service.reserve_items(7, 1, "cookie-a", "order-1", 1)

    with pytest.raises(CardInventoryError, match="商品") as error:
        service.release_reservation(reservation["reservation_id"], 1, 8, "cookie-a")

    assert error.value.code == "scope_mismatch"
    assert service.get_inventory_summary(7, 1, "cookie-a")["reserved"] == 1


def test_invalidated_items_do_not_occupy_capacity_or_return_to_available(inventory):
    service, manager = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=2)
    service.import_items(7, 1, "cookie-a", ["a", "b"])

    with manager.lock:
        manager.conn.execute(
            """
            UPDATE card_inventory_items
            SET status = 'invalidated', updated_at = CURRENT_TIMESTAMP
            WHERE secret_digest = ?
            """,
            (service._secret_digest("a"),),
        )
        manager.conn.commit()

    assert service.get_inventory_summary(7, 1, "cookie-a")["available"] == 1
    assert service.get_inventory_summary(7, 1, "cookie-a")["invalidated"] == 1
    assert service.generate_items(7, 1, "cookie-a")["generated"] == 1
    summary = service.get_inventory_summary(7, 1, "cookie-a")
    assert summary["available"] == 2
    assert summary["invalidated"] == 1


def test_concurrent_reservations_cannot_oversell(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=2)
    service.import_items(7, 1, "cookie-a", ["a", "b"])

    def reserve(order_id):
        try:
            return service.reserve_items(7, 1, "cookie-a", order_id, 2)
        except CardInventoryError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ["order-a", "order-b"]))

    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("insufficient_inventory") == 1
    assert service.get_inventory_summary(7, 1, "cookie-a")["reserved"] == 2
