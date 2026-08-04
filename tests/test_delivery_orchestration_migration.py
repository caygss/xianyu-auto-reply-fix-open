import sqlite3


def test_new_orchestration_table_defines_terminal_claim_token_canonically(
    monkeypatch,
    tmp_path,
):
    import db_manager as db_manager_module

    statements = []
    original_connect = db_manager_module.sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(db_manager_module.sqlite3, "connect", traced_connect)
    manager = db_manager_module.DBManager(str(tmp_path / "canonical.sqlite3"))
    try:
        create_statement = next(
            statement
            for statement in statements
            if "CREATE TABLE IF NOT EXISTS delivery_orchestration_states"
            in statement
        )
        assert "terminal_claim_token TEXT" in create_statement
        assert not any(
            "ALTER TABLE delivery_orchestration_states" in statement
            and "terminal_claim_token" in statement
            for statement in statements
        )
    finally:
        manager.close()


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
            "claim_token",
            "claimed_at",
            "terminal_claim_token",
        } <= names

        indexes = manager.conn.execute(
            "PRAGMA index_list(delivery_orchestration_states)"
        ).fetchall()
        assert any("order_line" in str(row[1]) for row in indexes)
    finally:
        manager.close()


def test_existing_orchestration_table_adds_claim_lease_columns(tmp_path):
    from db_manager import DBManager

    database_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE delivery_orchestration_states (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            card_id INTEGER NOT NULL,
            account_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            order_line_id TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            mode TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            reservation_id TEXT,
            status TEXT NOT NULL,
            result_meta TEXT NOT NULL DEFAULT '{}',
            last_error_code TEXT,
            last_error TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP,
            UNIQUE (user_id, card_id, account_id, order_id, order_line_id)
        )
        """
    )
    connection.commit()
    connection.close()

    manager = DBManager(str(database_path))
    try:
        columns = manager.conn.execute(
            "PRAGMA table_info(delivery_orchestration_states)"
        ).fetchall()
        assert {"claim_token", "claimed_at", "terminal_claim_token"} <= {
            row[1] for row in columns
        }
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
