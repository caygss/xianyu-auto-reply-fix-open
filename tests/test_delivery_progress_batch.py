import pytest

from db_manager import DBManager


@pytest.fixture
def manager(tmp_path):
    db = DBManager(str(tmp_path / "delivery-progress.sqlite3"))
    yield db
    db.close()


def record_state(manager, order_id, unit_index, status, delivery_meta):
    assert manager.upsert_delivery_finalization_state(
        order_id=order_id,
        unit_index=unit_index,
        status=status,
        delivery_meta=delivery_meta,
    )


def test_sent_configured_batch_counts_all_units_but_exposes_one_recovery_anchor(manager):
    record_state(
        manager,
        "order-sent-batch",
        1,
        "sent",
        {"configured": True, "quantity": 3, "delivery_unit_index": 1},
    )

    summary = manager.get_delivery_progress_summary("order-sent-batch", 3)

    assert summary["pending_finalize_count"] == 3
    assert summary["remaining_count"] == 0
    assert summary["pending_finalize_unit_indexes"] == [1]
    assert summary["finalized_count"] == 0
    assert summary["remaining_unit_indexes"] == []


def test_finalized_configured_batch_expands_coverage_and_ships_order(manager):
    record_state(manager, "order-finalized-batch", 1, "finalized", {})
    record_state(
        manager,
        "order-finalized-batch",
        2,
        "finalized",
        {"configured": True, "quantity": 3, "delivery_unit_index": 2},
    )

    summary = manager.get_delivery_progress_summary("order-finalized-batch", 4)

    assert summary["finalized_count"] == 4
    assert summary["finalized_unit_indexes"] == [1, 2, 3, 4]
    assert summary["pending_finalize_count"] == 0
    assert summary["remaining_count"] == 0
    assert summary["aggregate_status"] == "shipped"


@pytest.mark.parametrize("reverse_db_order", [False, True])
def test_configured_batch_overlap_preserves_explicit_unit_independent_of_db_order(
    manager,
    monkeypatch,
    reverse_db_order,
):
    order_id = f"order-explicit-overlap-{reverse_db_order}"
    record_state(
        manager,
        order_id,
        1,
        "sent",
        {"configured": True, "quantity": 3, "delivery_unit_index": 1},
    )
    record_state(manager, order_id, 2, "finalized", {})
    states = manager.get_delivery_finalization_states(order_id)
    monkeypatch.setattr(
        manager,
        "get_delivery_finalization_states",
        lambda requested_order_id: list(reversed(states))
        if reverse_db_order
        else list(states),
    )

    summary = manager.get_delivery_progress_summary(order_id, 3)

    assert summary["coverage_conflict"] is True
    assert summary["conflict_unit_indexes"] == [2]
    assert summary["finalized_unit_indexes"] == [2]
    assert summary["pending_finalize_count"] == 2
    assert summary["pending_finalize_unit_indexes"] == [1]
    assert summary["remaining_unit_indexes"] == []
    assert summary["aggregate_status"] == "pending_ship"


@pytest.mark.parametrize("reverse_db_order", [False, True])
def test_overlapping_configured_batches_report_stable_conflict_and_one_sent_anchor(
    manager,
    monkeypatch,
    reverse_db_order,
):
    order_id = f"order-batch-overlap-{reverse_db_order}"
    record_state(
        manager,
        order_id,
        1,
        "sent",
        {"configured": True, "quantity": 3, "delivery_unit_index": 1},
    )
    record_state(
        manager,
        order_id,
        2,
        "finalized",
        {"configured": True, "quantity": 3, "delivery_unit_index": 2},
    )
    states = manager.get_delivery_finalization_states(order_id)
    monkeypatch.setattr(
        manager,
        "get_delivery_finalization_states",
        lambda requested_order_id: list(reversed(states))
        if reverse_db_order
        else list(states),
    )

    summary = manager.get_delivery_progress_summary(order_id, 4)

    assert summary["coverage_conflict"] is True
    assert summary["conflict_unit_indexes"] == [2, 3]
    assert summary["pending_finalize_count"] == 1
    assert summary["pending_finalize_unit_indexes"] == [1]
    assert summary["finalized_unit_indexes"] == [2, 4]
    assert summary["remaining_unit_indexes"] == [3]
    assert summary["aggregate_status"] == "pending_ship"


def test_finalized_overlap_never_reports_shipped(manager):
    record_state(
        manager,
        "order-finalized-overlap",
        1,
        "finalized",
        {"configured": True, "quantity": 3, "delivery_unit_index": 1},
    )
    record_state(manager, "order-finalized-overlap", 2, "finalized", {})

    summary = manager.get_delivery_progress_summary("order-finalized-overlap", 3)

    assert summary["coverage_conflict"] is True
    assert summary["conflict_unit_indexes"] == [2]
    assert summary["finalized_count"] == 3
    assert summary["remaining_count"] == 0
    assert summary["aggregate_status"] == "pending_ship"


def test_legacy_per_unit_progress_is_unchanged(manager):
    record_state(manager, "order-legacy", 1, "finalized", {})
    record_state(
        manager,
        "order-legacy",
        2,
        "sent",
        {"configured": False, "quantity": 2, "delivery_unit_index": 2},
    )

    summary = manager.get_delivery_progress_summary("order-legacy", 3)

    assert summary["finalized_count"] == 1
    assert summary["pending_finalize_count"] == 1
    assert summary["remaining_count"] == 1
    assert summary["finalized_unit_indexes"] == [1]
    assert summary["pending_finalize_unit_indexes"] == [2]
    assert summary["remaining_unit_indexes"] == [3]
    assert summary["aggregate_status"] == "partial_pending_finalize"


@pytest.mark.parametrize("quantity", [0, -1, True, "invalid", 3])
def test_invalid_or_out_of_bounds_batch_quantity_falls_back_to_one(manager, quantity):
    order_id = f"order-invalid-batch-{quantity!r}"
    record_state(
        manager,
        order_id,
        2,
        "finalized",
        {"configured": True, "quantity": quantity, "delivery_unit_index": 2},
    )

    summary = manager.get_delivery_progress_summary(order_id, 3)

    assert summary["finalized_count"] == 1
    assert summary["finalized_unit_indexes"] == [2]
    assert summary["remaining_count"] == 2
    assert summary["remaining_unit_indexes"] == [1, 3]
    assert summary["aggregate_status"] == "partial_success"
