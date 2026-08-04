import json
import sqlite3

import pytest


def _create_legacy_delivery_tables(connection):
    connection.executescript(
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
        );
        CREATE TABLE orders (
            order_id TEXT PRIMARY KEY,
            item_id TEXT,
            buyer_id TEXT,
            sid TEXT,
            spec_name TEXT,
            spec_value TEXT,
            spec_name_2 TEXT,
            spec_value_2 TEXT,
            quantity TEXT,
            amount TEXT,
            bargain_flow_detected INTEGER DEFAULT 0,
            bargain_success_detected INTEGER DEFAULT 0,
            order_status TEXT DEFAULT 'unknown',
            pre_refund_status TEXT,
            platform_created_at TIMESTAMP,
            platform_paid_at TIMESTAMP,
            platform_completed_at TIMESTAMP,
            is_rated INTEGER DEFAULT 0,
            rated_at TIMESTAMP,
            rate_error TEXT,
            is_red_flower INTEGER DEFAULT 0,
            red_flower_at TIMESTAMP,
            red_flower_error TEXT,
            cookie_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
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
        );
        CREATE TABLE item_delivery_bindings (
            user_id INTEGER NOT NULL,
            account_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            card_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, account_id, item_id)
        );
        CREATE TABLE card_inventory_reservations (
            reservation_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            card_id INTEGER NOT NULL,
            account_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'reserved',
            idempotency_key TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            committed_at TIMESTAMP,
            released_at TIMESTAMP,
            order_line_id TEXT NOT NULL DEFAULT 'default',
            UNIQUE (user_id, card_id, account_id, order_id, order_line_id),
            UNIQUE (user_id, card_id, account_id, order_line_id, idempotency_key)
        );
        """
    )


def _insert_historical_orchestration(
    connection,
    *,
    status,
    suffix,
    card_id,
    reservation_id=None,
    account_id="account-a",
    order_id=None,
    order_line_id=None,
    idempotency_key=None,
):
    order_id = order_id or f"order-{suffix}"
    order_line_id = order_line_id or f"line-{suffix}"
    idempotency_key = idempotency_key or (
        f"1|{account_id}|{order_id}|{order_line_id}|{card_id}"
    )
    connection.execute(
        """
        INSERT INTO delivery_orchestration_states (
            user_id, card_id, account_id, order_id, order_line_id,
            quantity, mode, idempotency_key, reservation_id, claim_token,
            claimed_at, status, result_meta
        ) VALUES (1, ?, ?, ?, ?, 1, 'fixed_link', ?, ?, ?,
                  CURRENT_TIMESTAMP, ?, '{}')
        """,
        (
            card_id,
            account_id,
            order_id,
            order_line_id,
            idempotency_key,
            reservation_id,
            f"private-{suffix}" if status == "sending" else None,
            status,
        ),
    )
    if reservation_id:
        connection.execute(
            """
            INSERT INTO card_inventory_reservations (
                reservation_id, user_id, card_id, account_id, order_id,
                quantity, status, idempotency_key, order_line_id
            ) VALUES (?, 1, ?, ?, ?, 1, 'committed', ?, ?)
            """,
            (
                reservation_id,
                card_id,
                account_id,
                order_id,
                idempotency_key,
                order_line_id,
            ),
        )
    return order_id, order_line_id, idempotency_key


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
            "item_scope_migration_version INTEGER NOT NULL DEFAULT 1",
        ):
            assert column_definition in create_statement
        for column_name in (
            "terminal_claim_token",
            "item_id",
            "send_started_at",
            "verification_required",
            "item_scope_migration_version",
        ):
            assert not any(
                "ALTER TABLE delivery_orchestration_states" in statement
                and column_name in statement
                for statement in statements
            )
        marker_index = manager.conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'index'
              AND name = 'idx_delivery_orchestration_item_scope_migration'
            """
        ).fetchone()
        assert marker_index is not None
        assert "WHERE item_scope_migration_version < 1" in marker_index[0]
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
        marker_column = next(
            row for row in columns if row[1] == "item_scope_migration_version"
        )
        assert marker_column[3] == 1
        assert str(marker_column[4]).strip("'\"") == "1"
        marker_index = manager.conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'index'
              AND name = 'idx_delivery_orchestration_item_scope_migration'
            """
        ).fetchone()
        assert marker_index is not None
        assert "WHERE item_scope_migration_version < 1" in marker_index[0]
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


def test_old_database_backfills_unique_item_scope_for_all_historical_statuses(
    tmp_path,
):
    from card_inventory_service import CardInventoryService
    from db_manager import DBManager
    from delivery_orchestration_service import (
        DeliveryOrchestrationRequest,
        DeliveryOrchestrationService,
    )
    from test_delivery_quantity_contract import FakeDispatcher

    database_path = tmp_path / "historical-resolvable.sqlite3"
    connection = sqlite3.connect(database_path)
    _create_legacy_delivery_tables(connection)
    rows = {}
    for status, card_id in (("failed", 7), ("paused", 8), ("sent", 9), ("sending", 10)):
        rows[status] = _insert_historical_orchestration(
            connection,
            status=status,
            suffix=f"resolved-{status}",
            card_id=card_id,
            reservation_id="reservation-resolved-failed" if status == "failed" else None,
        )

    connection.execute(
        "INSERT INTO orders(order_id, item_id, cookie_id) VALUES (?, ?, 'account-a')",
        (rows["failed"][0], "item-resolved-failed"),
    )
    connection.execute(
        "INSERT INTO orders(order_id, item_id, cookie_id) VALUES (?, ?, 'account-a')",
        (rows["sending"][0], "item-resolved-sending"),
    )
    connection.execute(
        """
        INSERT INTO delivery_finalization_states(
            order_id, unit_index, cookie_id, item_id, status, delivery_meta
        ) VALUES (?, 1, 'account-a', ?, 'sent', ?)
        """,
        (
            rows["sending"][0],
            "item-resolved-sending",
            json.dumps(
                {
                    "configured": True,
                    "idempotency_key": rows["sending"][2],
                }
            ),
        ),
    )
    connection.execute(
        """
        INSERT INTO delivery_finalization_states(
            order_id, unit_index, cookie_id, item_id, status, delivery_meta
        ) VALUES (?, 1, 'account-a', ?, 'finalized', ?)
        """,
        (
            rows["paused"][0],
            "item-resolved-paused",
            json.dumps({"idempotency_key": rows["paused"][2]}),
        ),
    )
    connection.execute(
        """
        INSERT INTO item_delivery_bindings(user_id, account_id, item_id, card_id)
        VALUES (1, 'account-a', 'item-resolved-sent', 9)
        """
    )
    connection.commit()
    connection.close()

    manager = DBManager(str(database_path))
    try:
        migrated = {
            row[0]: row[1:]
            for row in manager.conn.execute(
                """
                SELECT status, item_id, verification_required, send_started_at,
                       reservation_id
                FROM delivery_orchestration_states
                """
            ).fetchall()
        }
        assert migrated["failed"] == (
            "item-resolved-failed",
            0,
            None,
            "reservation-resolved-failed",
        )
        assert migrated["paused"][:3] == ("item-resolved-paused", 0, None)
        assert migrated["sent"][:3] == ("item-resolved-sent", 0, None)
        assert migrated["sending"][0] == "item-resolved-sending"
        assert migrated["sending"][1] == 1
        assert migrated["sending"][2] is not None
        sending_finalization_meta = json.loads(
            manager.conn.execute(
                """
                SELECT delivery_meta FROM delivery_finalization_states
                WHERE order_id = ?
                """,
                (rows["sending"][0],),
            ).fetchone()[0]
        )
        assert sending_finalization_meta["claim_verification_required"] is True

        inventory = CardInventoryService(manager)
        dispatcher = FakeDispatcher(inventory)
        service = DeliveryOrchestrationService(manager, inventory, dispatcher)

        def request_for(status, item_id, card_id):
            order_id, order_line_id, idempotency_key = rows[status]
            return DeliveryOrchestrationRequest(
                user_id=1,
                card_id=card_id,
                account_id="account-a",
                order_id=order_id,
                order_line_id=order_line_id,
                quantity=1,
                delivery_config={
                    "mode": "fixed_link",
                    "url": "https://example.test/historical",
                },
                item_id=item_id,
                idempotency_key=idempotency_key,
            )

        failed_retry = service.prepare_retry(
            request_for("failed", "item-resolved-failed", 7)
        )
        paused_retry = service.prepare_retry(
            request_for("paused", "item-resolved-paused", 8)
        )
        prepare_count_before_sent = len(dispatcher.requests)
        sent_retry = service.prepare_retry(
            request_for("sent", "item-resolved-sent", 9)
        )
        sending_retry = service.prepare_retry(
            request_for("sending", "item-resolved-sending", 10)
        )

        assert failed_retry["status"] == "sending"
        assert failed_retry["reservation_id"] == "reservation-resolved-failed"
        assert paused_retry["status"] == "sending"
        assert paused_retry["claimed"] is True
        assert sent_retry["status"] == "sent"
        assert sent_retry.get("claimed") is not True
        assert len(dispatcher.requests) == prepare_count_before_sent
        assert sending_retry["verification_required"] is True
        assert sending_retry.get("claimed") is not True
        assert manager.conn.execute(
            "SELECT COUNT(*) FROM card_inventory_reservations"
        ).fetchone()[0] == 1
    finally:
        manager.close()


def test_old_database_quarantines_unresolved_and_conflicting_item_scopes(
    tmp_path,
):
    from card_inventory_service import CardInventoryService
    from db_manager import DBManager
    from delivery_orchestration_service import (
        DeliveryOrchestrationRequest,
        DeliveryOrchestrationService,
    )
    from test_delivery_quantity_contract import FakeDispatcher

    database_path = tmp_path / "historical-unverified.sqlite3"
    connection = sqlite3.connect(database_path)
    _create_legacy_delivery_tables(connection)
    rows = {}
    for status, card_id in (("failed", 20), ("paused", 21), ("sent", 22), ("sending", 23)):
        rows[status] = _insert_historical_orchestration(
            connection,
            status=status,
            suffix=f"unverified-{status}",
            card_id=card_id,
            reservation_id=f"reservation-unverified-{status}",
        )

    connection.execute(
        "INSERT INTO orders(order_id, item_id, cookie_id) VALUES (?, ?, 'account-a')",
        (rows["paused"][0], "item-conflict-from-order"),
    )
    connection.execute(
        """
        INSERT INTO delivery_finalization_states(
            order_id, unit_index, cookie_id, item_id, status, delivery_meta
        ) VALUES (?, 1, 'account-a', ?, 'finalized', ?)
        """,
        (
            rows["paused"][0],
            "item-conflict-from-finalization",
            json.dumps({"idempotency_key": rows["paused"][2]}),
        ),
    )
    connection.execute(
        """
        INSERT INTO delivery_finalization_states(
            order_id, unit_index, cookie_id, item_id, status, delivery_meta
        ) VALUES (?, 1, 'account-a', NULL, 'sent', ?)
        """,
        (
            rows["sent"][0],
            json.dumps(
                {
                    "configured": True,
                    "idempotency_key": rows["sent"][2],
                }
            ),
        ),
    )
    connection.commit()
    connection.close()

    manager = DBManager(str(database_path))
    try:
        migrated = manager.conn.execute(
            """
            SELECT status, item_id, verification_required, send_started_at,
                   last_error_code, last_error, reservation_id
            FROM delivery_orchestration_states
            ORDER BY card_id
            """
        ).fetchall()
        assert [row[0] for row in migrated] == ["failed", "paused", "sent", "sending"]
        assert all(row[1] is None for row in migrated)
        assert all(row[2] == 1 and row[3] is not None for row in migrated)
        assert all(row[4] == "historical_item_scope_unverified" for row in migrated)
        assert all("历史发货商品范围" in row[5] for row in migrated)
        assert {row[6] for row in migrated} == {
            "reservation-unverified-failed",
            "reservation-unverified-paused",
            "reservation-unverified-sent",
            "reservation-unverified-sending",
        }

        sent_meta = json.loads(
            manager.conn.execute(
                """
                SELECT delivery_meta FROM delivery_finalization_states
                WHERE order_id = ?
                """,
                (rows["sent"][0],),
            ).fetchone()[0]
        )
        assert sent_meta["claim_verification_required"] is True
        assert "claim_token" not in sent_meta

        inventory = CardInventoryService(manager)
        dispatcher = FakeDispatcher(inventory)
        service = DeliveryOrchestrationService(manager, inventory, dispatcher)
        for status, card_id in (("failed", 20), ("paused", 21), ("sent", 22), ("sending", 23)):
            order_id, order_line_id, idempotency_key = rows[status]
            request = DeliveryOrchestrationRequest(
                user_id=1,
                card_id=card_id,
                account_id="account-a",
                order_id=order_id,
                order_line_id=order_line_id,
                quantity=1,
                delivery_config={
                    "mode": "fixed_link",
                    "url": "https://example.test/historical",
                },
                item_id=f"runtime-item-{status}",
                idempotency_key=idempotency_key,
            )
            result = service.prepare_retry(request)
            assert result["verification_required"] is True
            assert result.get("claimed") is not True
            assert result["error_code"] == "historical_item_scope_unverified"

        assert dispatcher.requests == []
        assert manager.conn.execute(
            "SELECT COUNT(*) FROM card_inventory_reservations"
        ).fetchone()[0] == 4
    finally:
        manager.close()


def test_historical_anchor_cannot_cross_account_with_forged_idempotency_key(
    tmp_path,
):
    from db_manager import DBManager

    database_path = tmp_path / "cross-account-anchor.sqlite3"
    connection = sqlite3.connect(database_path)
    _create_legacy_delivery_tables(connection)
    account_a = _insert_historical_orchestration(
        connection,
        status="failed",
        suffix="cross-account-a",
        card_id=7,
        account_id="account-a",
        order_id="order-a",
        order_line_id="line-a",
    )
    _insert_historical_orchestration(
        connection,
        status="failed",
        suffix="cross-account-b",
        card_id=8,
        account_id="account-b",
        order_id="order-b",
        order_line_id="line-b",
    )
    connection.execute(
        """
        INSERT INTO orders(order_id, item_id, cookie_id)
        VALUES ('order-a', 'item-a', 'account-a')
        """
    )
    original_meta = json.dumps(
        {
            "configured": True,
            "idempotency_key": account_a[2],
            "claim_verification_required": True,
            "audit_note": "belongs only to account-b/order-b",
        },
        separators=(", ", ": "),
    )
    connection.execute(
        """
        INSERT INTO delivery_finalization_states(
            order_id, unit_index, cookie_id, item_id, status, delivery_meta
        ) VALUES ('order-b', 1, 'account-b', 'item-b', 'sent', ?)
        """,
        (original_meta,),
    )
    connection.commit()
    connection.close()

    manager = DBManager(str(database_path))
    try:
        rows = manager.conn.execute(
            """
            SELECT account_id, item_id, verification_required
            FROM delivery_orchestration_states
            ORDER BY account_id
            """
        ).fetchall()
        stored_meta = manager.conn.execute(
            "SELECT delivery_meta FROM delivery_finalization_states"
        ).fetchone()[0]

        assert rows == [
            ("account-a", "item-a", 0),
            ("account-b", None, 1),
        ]
        assert stored_meta == original_meta
    finally:
        manager.close()


def test_partial_scope_anchor_cannot_resolve_multiple_order_lines(tmp_path):
    from db_manager import DBManager

    database_path = tmp_path / "partial-scope-anchor.sqlite3"
    connection = sqlite3.connect(database_path)
    _create_legacy_delivery_tables(connection)
    for line in ("line-1", "line-2"):
        _insert_historical_orchestration(
            connection,
            status="failed",
            suffix=f"partial-{line}",
            card_id=7,
            order_id="order-shared",
            order_line_id=line,
        )
    original_meta = '{"configured": true, "card_id": 7, "audit_note": "no line"}'
    connection.execute(
        """
        INSERT INTO delivery_finalization_states(
            order_id, unit_index, cookie_id, item_id, status, delivery_meta
        ) VALUES ('order-shared', 1, 'account-a', 'item-partial', 'sent', ?)
        """,
        (original_meta,),
    )
    connection.commit()
    connection.close()

    manager = DBManager(str(database_path))
    try:
        rows = manager.conn.execute(
            """
            SELECT order_line_id, item_id, verification_required
            FROM delivery_orchestration_states
            ORDER BY order_line_id
            """
        ).fetchall()
        stored_meta = manager.conn.execute(
            "SELECT delivery_meta FROM delivery_finalization_states"
        ).fetchone()[0]

        assert rows == [
            ("line-1", None, 1),
            ("line-2", None, 1),
        ]
        assert stored_meta == original_meta
    finally:
        manager.close()


def test_multiple_exact_anchors_do_not_resolve_or_rewrite_historical_state(
    tmp_path,
):
    from db_manager import DBManager

    database_path = tmp_path / "multiple-exact-anchors.sqlite3"
    connection = sqlite3.connect(database_path)
    _create_legacy_delivery_tables(connection)
    state = _insert_historical_orchestration(
        connection,
        status="failed",
        suffix="multiple-exact",
        card_id=7,
        order_id="order-multiple",
        order_line_id="line-multiple",
    )
    original_metas = []
    for unit_index, item_id in ((1, "item-one"), (2, "item-two")):
        raw_meta = json.dumps(
            {
                "configured": True,
                "idempotency_key": state[2],
                "audit_unit": unit_index,
            },
            separators=(", ", ": "),
        )
        original_metas.append(raw_meta)
        connection.execute(
            """
            INSERT INTO delivery_finalization_states(
                order_id, unit_index, cookie_id, item_id, status, delivery_meta
            ) VALUES ('order-multiple', ?, 'account-a', ?, 'sent', ?)
            """,
            (unit_index, item_id, raw_meta),
        )
    connection.commit()
    connection.close()

    manager = DBManager(str(database_path))
    try:
        row = manager.conn.execute(
            """
            SELECT item_id, verification_required
            FROM delivery_orchestration_states
            """
        ).fetchone()
        stored_metas = [
            value[0]
            for value in manager.conn.execute(
                """
                SELECT delivery_meta FROM delivery_finalization_states
                ORDER BY unit_index
                """
            ).fetchall()
        ]

        assert row == (None, 1)
        assert stored_metas == original_metas
    finally:
        manager.close()


def test_complete_finalization_scope_uniquely_backfills_only_one_order_line(
    tmp_path,
):
    from db_manager import DBManager

    database_path = tmp_path / "complete-scope-anchor.sqlite3"
    connection = sqlite3.connect(database_path)
    _create_legacy_delivery_tables(connection)
    for line in ("line-1", "line-2"):
        _insert_historical_orchestration(
            connection,
            status="failed",
            suffix=f"complete-{line}",
            card_id=7,
            order_id="order-complete",
            order_line_id=line,
        )
    original_meta = json.dumps(
        {
            "configured": True,
            "card_id": 7,
            "order_line_id": "line-1",
            "audit_note": "complete owner scope",
        },
        separators=(", ", ": "),
    )
    connection.execute(
        """
        INSERT INTO delivery_finalization_states(
            order_id, unit_index, cookie_id, item_id, status, delivery_meta
        ) VALUES ('order-complete', 1, 'account-a', 'item-line-1', 'sent', ?)
        """,
        (original_meta,),
    )
    connection.commit()
    connection.close()

    manager = DBManager(str(database_path))
    try:
        rows = manager.conn.execute(
            """
            SELECT order_line_id, item_id, verification_required
            FROM delivery_orchestration_states
            ORDER BY order_line_id
            """
        ).fetchall()
        stored_meta = manager.conn.execute(
            "SELECT delivery_meta FROM delivery_finalization_states"
        ).fetchone()[0]

        assert rows == [
            ("line-1", "item-line-1", 0),
            ("line-2", None, 1),
        ]
        assert stored_meta == original_meta
    finally:
        manager.close()


def test_unresolved_failed_sibling_does_not_quarantine_exact_sent_anchor_on_restart(
    tmp_path,
):
    from card_inventory_service import CardInventoryService
    from db_manager import DBManager
    from delivery_orchestration_service import (
        DeliveryOrchestrationRequest,
        DeliveryOrchestrationService,
    )
    from test_delivery_quantity_contract import FakeDispatcher

    database_path = tmp_path / "sibling-restart-isolation.sqlite3"
    connection = sqlite3.connect(database_path)
    _create_legacy_delivery_tables(connection)
    sent_state = _insert_historical_orchestration(
        connection,
        status="sent",
        suffix="sibling-sent",
        card_id=7,
        order_id="order-siblings",
        order_line_id="line-sent",
    )
    failed_state = _insert_historical_orchestration(
        connection,
        status="failed",
        suffix="sibling-failed",
        card_id=7,
        order_id="order-siblings",
        order_line_id="line-failed",
        reservation_id="reservation-sibling-failed",
    )
    original_meta = json.dumps(
        {
            "configured": True,
            "idempotency_key": sent_state[2],
            "audit_note": "exact sent sibling",
        },
        separators=(", ", ": "),
    )
    connection.execute(
        """
        INSERT INTO delivery_finalization_states(
            order_id, unit_index, cookie_id, item_id, status, delivery_meta
        ) VALUES ('order-siblings', 1, 'account-a', 'item-sent', 'sent', ?)
        """,
        (original_meta,),
    )
    ambiguous_meta = (
        '{"configured": true, "card_id": 7, '
        '"audit_note": "ambiguous sibling anchor"}'
    )
    connection.execute(
        """
        INSERT INTO delivery_finalization_states(
            order_id, unit_index, cookie_id, item_id, status, delivery_meta
        ) VALUES ('order-siblings', 2, 'account-a', 'item-ambiguous', 'sent', ?)
        """,
        (ambiguous_meta,),
    )
    connection.commit()
    connection.close()

    first_start = DBManager(str(database_path))
    first_start.close()
    second_start = DBManager(str(database_path))
    try:
        rows = {
            row[0]: row[1:]
            for row in second_start.conn.execute(
                """
                SELECT order_line_id, item_id, verification_required, reservation_id
                FROM delivery_orchestration_states
                """
            ).fetchall()
        }
        stored_metas = [
            row[0]
            for row in second_start.conn.execute(
                """
                SELECT delivery_meta FROM delivery_finalization_states
                ORDER BY unit_index
                """
            ).fetchall()
        ]

        assert rows["line-sent"] == ("item-sent", 0, None)
        assert rows["line-failed"] == (
            None,
            1,
            "reservation-sibling-failed",
        )
        assert stored_metas == [original_meta, ambiguous_meta]

        inventory = CardInventoryService(second_start)
        dispatcher = FakeDispatcher(inventory)
        service = DeliveryOrchestrationService(
            second_start,
            inventory,
            dispatcher,
        )

        def request_for(state, line_id, item_id):
            return DeliveryOrchestrationRequest(
                user_id=1,
                card_id=7,
                account_id="account-a",
                order_id="order-siblings",
                order_line_id=line_id,
                quantity=1,
                delivery_config={
                    "mode": "fixed_link",
                    "url": "https://example.test/historical",
                },
                item_id=item_id,
                idempotency_key=state[2],
            )

        sent_result = service.prepare_retry(
            request_for(sent_state, "line-sent", "item-sent")
        )
        failed_result = service.prepare_retry(
            request_for(failed_state, "line-failed", "runtime-item")
        )

        assert sent_result["status"] == "sent"
        assert sent_result["verification_required"] is False
        assert failed_result["verification_required"] is True
        assert failed_result["reservation_id"] == "reservation-sibling-failed"
        assert dispatcher.requests == []
    finally:
        second_start.close()


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
        assert "item_scope_migration_version" not in columns
        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(delivery_orchestration_states)"
            ).fetchall()
        }
        assert "idx_delivery_orchestration_item_scope_migration" not in indexes
    finally:
        connection.close()


def test_ten_thousand_isolated_rows_skip_source_loading_on_second_startup(
    monkeypatch,
    tmp_path,
):
    from db_manager import DBManager

    database_path = tmp_path / "large-historical-migration.sqlite3"
    connection = sqlite3.connect(database_path)
    _create_legacy_delivery_tables(connection)
    rows = [
        (
            1,
            7,
            "account-a",
            f"order-scale-{index}",
            f"line-scale-{index}",
            1,
            "fixed_link",
            f"1|account-a|order-scale-{index}|line-scale-{index}|7",
            "failed",
        )
        for index in range(10_000)
    ]
    connection.executemany(
        """
        INSERT INTO delivery_orchestration_states(
            user_id, card_id, account_id, order_id, order_line_id,
            quantity, mode, idempotency_key, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    connection.commit()
    connection.close()

    first_start = DBManager(str(database_path))
    try:
        migrated = first_start.conn.execute(
            """
            SELECT COUNT(*), SUM(verification_required),
                   SUM(item_scope_migration_version)
            FROM delivery_orchestration_states
            """
        ).fetchone()
        assert migrated == (10_000, 10_000, 10_000)
    finally:
        first_start.close()

    real_migration = DBManager._migrate_delivery_orchestration_send_state
    migration_events = {
        "source_loads": 0,
        "state_updates": 0,
        "selects": 0,
    }

    def traced_second_migration(manager, cursor):
        def trace(statement):
            normalized = " ".join(str(statement).split()).upper()
            if normalized.startswith("SELECT"):
                migration_events["selects"] += 1
            if any(
                source in normalized
                for source in (
                    "FROM DELIVERY_FINALIZATION_STATES",
                    "FROM ORDERS",
                    "FROM ITEM_DELIVERY_BINDINGS",
                )
            ):
                migration_events["source_loads"] += 1
            if normalized.startswith("UPDATE DELIVERY_ORCHESTRATION_STATES"):
                migration_events["state_updates"] += 1

        manager.conn.set_trace_callback(trace)
        try:
            return real_migration(manager, cursor)
        finally:
            manager.conn.set_trace_callback(None)

    monkeypatch.setattr(
        DBManager,
        "_migrate_delivery_orchestration_send_state",
        traced_second_migration,
    )

    second_start = DBManager(str(database_path))
    try:
        assert migration_events["source_loads"] == 0
        assert migration_events["state_updates"] == 0
        assert migration_events["selects"] <= 2
    finally:
        second_start.close()


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
