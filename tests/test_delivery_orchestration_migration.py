def test_delivery_orchestration_state_table_has_scoped_unique_key(tmp_path):
    from db_manager import DBManager

    manager = DBManager(str(tmp_path / "delivery.sqlite3"))
    try:
        columns = manager.conn.execute(
            "PRAGMA table_info(delivery_orchestration_states)"
        ).fetchall()
        names = {row[1] for row in columns}
        assert {
            "user_id",
            "card_id",
            "account_id",
            "order_id",
            "order_line_id",
            "quantity",
            "mode",
            "status",
            "idempotency_key",
            "reservation_id",
        } <= names

        indexes = manager.conn.execute(
            "PRAGMA index_list(delivery_orchestration_states)"
        ).fetchall()
        assert any("order_line" in str(row[1]) for row in indexes)
    finally:
        manager.close()


def test_card_inventory_reservation_scope_includes_order_line(tmp_path):
    from db_manager import DBManager

    manager = DBManager(str(tmp_path / "delivery.sqlite3"))
    try:
        columns = manager.conn.execute(
            "PRAGMA table_info(card_inventory_reservations)"
        ).fetchall()
        assert "order_line_id" in {row[1] for row in columns}
        indexes = manager.conn.execute(
            "PRAGMA index_list(card_inventory_reservations)"
        ).fetchall()
        assert any("line" in str(row[1]) for row in indexes)
    finally:
        manager.close()
