import sqlite3

from db_manager import DBManager


def test_new_database_creates_card_inventory_tables_and_indexes(tmp_path):
    manager = DBManager(str(tmp_path / "inventory.sqlite3"))

    with sqlite3.connect(manager.db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "card_inventory_items",
            "card_inventory_settings",
            "card_inventory_reservations",
        } <= tables
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(card_inventory_items)"
            )
        }
        assert {
            "user_id",
            "card_id",
            "account_id",
            "secret_text",
            "secret_digest",
            "source_type",
            "status",
            "order_id",
            "reservation_id",
            "unit_index",
            "idempotency_key",
            "created_at",
            "updated_at",
            "reserved_at",
            "delivered_at",
        } <= columns

    manager.close()


def test_inventory_migration_is_idempotent_and_preserves_existing_tables(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    manager = DBManager(str(db_path))
    with manager.lock:
        manager.conn.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
        manager.conn.execute("INSERT INTO legacy_marker(value) VALUES ('keep')")
        manager.conn.commit()
        manager.init_db()
    manager.close()

    restarted = DBManager(str(db_path))
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT value FROM legacy_marker"
        ).fetchone() == ("keep",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'card_inventory_reservations'"
        ).fetchone() == ("card_inventory_reservations",)
    restarted.close()
