import json
import sqlite3

import pytest


def test_new_orchestration_table_defines_send_safety_columns_canonically(
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
        for column_definition in (
            "terminal_claim_token TEXT",
            "item_id TEXT",
            "send_started_at TIMESTAMP",
            "verification_required INTEGER NOT NULL DEFAULT 0",
        ):
            assert column_definition in create_statement
        for column_name in (
            "terminal_claim_token",
            "item_id",
            "send_started_at",
            "verification_required",
        ):
            assert not any(
                "ALTER TABLE delivery_orchestration_states" in statement
                and column_name in statement
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


def test_existing_database_migrates_historical_uncertain_delivery_atomically(
    tmp_path,
):
    from db_manager import DBManager

    database_path = tmp_path / "historical-uncertain.sqlite3"
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
            claim_token TEXT,
            claimed_at TIMESTAMP,
            terminal_claim_token TEXT,
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
    idempotency_key = "1|account-a|order-historical-uncertain|item-bound|7"
    connection.execute(
        """
        INSERT INTO delivery_orchestration_states (
            user_id, card_id, account_id, order_id, order_line_id,
            quantity, mode, idempotency_key, claim_token, claimed_at,
            status, result_meta
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'sending', '{}')
        """,
        (
            1,
            7,
            "account-a",
            "order-historical-uncertain",
            "item-bound",
            1,
            "fixed_link",
            idempotency_key,
            "historical-private-token",
        ),
    )
    connection.execute(
        """
        CREATE TABLE delivery_finalization_states (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            unit_index INTEGER NOT NULL DEFAULT 1,
            cookie_id TEXT,
            item_id TEXT,
            buyer_id TEXT,
            channel TEXT NOT NULL DEFAULT 'auto',
            status TEXT NOT NULL DEFAULT 'sent',
            delivery_meta TEXT,
            last_error TEXT,
            sent_at TIMESTAMP,
            finalized_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(order_id, unit_index)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO delivery_finalization_states (
            order_id, unit_index, cookie_id, item_id, status, delivery_meta
        ) VALUES (?, 1, ?, ?, 'sent', ?)
        """,
        (
            "order-historical-uncertain",
            "account-a",
            "item-bound",
            json.dumps(
                {
                    "configured": True,
                    "claim_verification_required": True,
                    "idempotency_key": idempotency_key,
                }
            ),
        ),
    )
    connection.commit()
    connection.close()

    manager = DBManager(str(database_path))
    try:
        columns = {
            row[1]
            for row in manager.conn.execute(
                "PRAGMA table_info(delivery_orchestration_states)"
            ).fetchall()
        }
        assert {"item_id", "send_started_at", "verification_required"} <= columns
        row = manager.conn.execute(
            """
            SELECT item_id, send_started_at, verification_required
            FROM delivery_orchestration_states
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        assert row[0] == "item-bound"
        assert row[1] is not None
        assert row[2] == 1
    finally:
        manager.close()


def test_send_state_migration_rolls_back_all_columns_on_failure(
    monkeypatch,
    tmp_path,
):
    from db_manager import DBManager

    database_path = tmp_path / "migration-rollback.sqlite3"
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
            sent_at TIMESTAMP
        )
        """
    )
    connection.commit()
    connection.close()

    real_migration = DBManager._migrate_delivery_orchestration_send_state

    def fail_after_migration(manager, cursor):
        real_migration(manager, cursor)
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(
        DBManager,
        "_migrate_delivery_orchestration_send_state",
        fail_after_migration,
    )

    with pytest.raises(RuntimeError, match="injected migration failure"):
        DBManager(str(database_path))

    connection = sqlite3.connect(database_path)
    try:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(delivery_orchestration_states)"
            ).fetchall()
        }
        assert "item_id" not in columns
        assert "send_started_at" not in columns
        assert "verification_required" not in columns
    finally:
        connection.close()


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
