import sqlite3

import pytest

from db_manager import DBManager, ITEM_DELIVERY_BINDING_MARKER


def _make_legacy_cards_database(tmp_path, *, invalid_user=False):
    db_path = tmp_path / "legacy-cards.sqlite3"
    manager = DBManager(str(db_path))
    manager.close()

    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP TABLE cards")
    connection.execute(
        """
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('api', 'text', 'data', 'image')),
            api_config TEXT,
            text_content TEXT,
            data_content TEXT,
            image_url TEXT,
            description TEXT,
            enabled BOOLEAN DEFAULT TRUE,
            delay_seconds INTEGER DEFAULT 0,
            is_multi_spec BOOLEAN DEFAULT FALSE,
            spec_name TEXT,
            spec_value TEXT,
            spec_name_2 TEXT,
            spec_value_2 TEXT,
            user_id INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    card_user_id = 999 if invalid_user else 1
    connection.execute(
        """
        INSERT INTO cards (id, name, type, description, user_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            41,
            "历史绑定卡券",
            "text",
            f"{ITEM_DELIVERY_BINDING_MARKER} legacy",
            card_user_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO item_delivery_bindings (user_id, account_id, item_id, card_id)
        VALUES (?, ?, ?, ?)
        """,
        (1, "account-a", "item-legacy", 41),
    )
    connection.execute(
        """
        INSERT INTO delivery_rules (keyword, card_id, description)
        VALUES (?, ?, ?)
        """,
        ("legacy-keyword", 41, "legacy rule"),
    )
    connection.execute(
        """
        INSERT INTO data_card_reservations (
            card_id, order_id, reserved_content
        ) VALUES (?, ?, ?)
        """,
        (41, "legacy-order", "legacy-secret"),
    )
    connection.commit()
    connection.close()
    return db_path


def test_cards_constraint_migration_preserves_all_card_references(tmp_path):
    db_path = _make_legacy_cards_database(tmp_path)

    manager = DBManager(str(db_path))
    try:
        assert manager.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert manager.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert manager.conn.execute(
            "SELECT card_id FROM item_delivery_bindings"
        ).fetchone()[0] == 41
        assert manager.conn.execute(
            "SELECT card_id FROM delivery_rules"
        ).fetchone()[0] == 41
        assert manager.conn.execute(
            "SELECT card_id FROM data_card_reservations"
        ).fetchone()[0] == 41
        assert manager.conn.execute(
            "SELECT type FROM cards WHERE id = 41"
        ).fetchone()[0] == "text"
        assert manager.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'cards_new'"
        ).fetchone() is None
    finally:
        manager.close()


def test_cards_constraint_migration_failure_rolls_back_without_temp_table(tmp_path):
    db_path = _make_legacy_cards_database(tmp_path, invalid_user=True)
    manager = None

    try:
        with pytest.raises(RuntimeError, match="cards表约束迁移失败"):
            manager = DBManager(str(db_path))
    finally:
        if manager is not None:
            manager.close()

    connection = sqlite3.connect(str(db_path))
    try:
        assert connection.execute(
            "SELECT name FROM cards WHERE id = 41"
        ).fetchone()[0] == "历史绑定卡券"
        assert connection.execute(
            "SELECT COUNT(*) FROM item_delivery_bindings WHERE card_id = 41"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM delivery_rules WHERE card_id = 41"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM data_card_reservations WHERE card_id = 41"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'cards_new'"
        ).fetchone() is None
    finally:
        connection.close()
