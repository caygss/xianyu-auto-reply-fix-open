import sqlite3

import pytest

import db_manager
from db_manager import (
    DBManager,
    ITEM_DELIVERY_BINDING_MARKER,
    _CardsTableMigrationError,
)


def _inject_cards_migration_connection_fault(monkeypatch, fault):
    real_connect = sqlite3.connect

    class FaultInjectingCursor:
        def __init__(self, cursor, connection):
            self._cursor = cursor
            self._connection = connection

        def execute(self, sql, *args, **kwargs):
            result = self._cursor.execute(sql, *args, **kwargs)
            normalized_sql = " ".join(sql.split()).upper()
            if normalized_sql == (
                "RELEASE SAVEPOINT CARDS_YIFAN_API_CONSTRAINT_PROBE"
            ):
                self._connection.fail_next_migration_commit = True
            return result

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class FaultInjectingConnection:
        def __init__(self, connection):
            self._connection = connection
            self.fail_next_migration_commit = False
            self.foreign_keys_on_count = 0
            self.foreign_keys_read_count = 0

        def cursor(self, *args, **kwargs):
            return FaultInjectingCursor(
                self._connection.cursor(*args, **kwargs),
                self,
            )

        def commit(self):
            if fault == "pre_migration_commit" and self.fail_next_migration_commit:
                self.fail_next_migration_commit = False
                raise sqlite3.DatabaseError(
                    "injected pre-migration commit failure"
                )
            return self._connection.commit()

        def execute(self, sql, *args, **kwargs):
            normalized_sql = " ".join(sql.split()).upper()
            if normalized_sql == "PRAGMA FOREIGN_KEYS = ON":
                self.foreign_keys_on_count += 1
                if (
                    fault == "restore_foreign_keys"
                    and self.foreign_keys_on_count == 2
                ):
                    raise sqlite3.DatabaseError(
                        "injected foreign-key restore failure"
                    )
            elif normalized_sql == "PRAGMA FOREIGN_KEYS":
                self.foreign_keys_read_count += 1
                if (
                    fault == "verify_restored_foreign_keys"
                    and self.foreign_keys_read_count == 2
                ):
                    raise sqlite3.DatabaseError(
                        "injected foreign-key verification failure"
                    )
            return self._connection.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    def connect_with_fault(*args, **kwargs):
        return FaultInjectingConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", connect_with_fault)


def _make_current_cards_database_without_user_one(tmp_path):
    db_path = tmp_path / "current-cards.sqlite3"
    manager = DBManager(str(db_path))
    manager.close()

    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("UPDATE users SET id = 2 WHERE id = 1")
    connection.execute(
        """
        INSERT INTO cards (
            id, name, type, is_multi_spec, spec_name, spec_value,
            spec_name_2, spec_value_2, user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (51, "current two-spec card", "text", 1, "color", "blue", "size", "large", 2),
    )
    cards_rootpage = connection.execute(
        "SELECT rootpage FROM sqlite_master WHERE type = 'table' AND name = 'cards'"
    ).fetchone()[0]
    cards_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cards'"
    ).fetchone()[0]
    assert "yifan_api" in cards_sql
    connection.commit()
    connection.close()
    return db_path, cards_rootpage


def _make_legacy_cards_database(
    tmp_path,
    *,
    invalid_user=False,
    type_constraint="type IN ('api', 'text', 'data', 'image')",
    required_probe_guard=False,
):
    db_path = tmp_path / "legacy-cards.sqlite3"
    manager = DBManager(str(db_path))
    manager.close()

    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP TABLE cards")
    probe_guard_column = "probe_guard TEXT NOT NULL," if required_probe_guard else ""
    connection.execute(
        f"""
        CREATE TABLE cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK ({type_constraint}),
            {probe_guard_column}
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
    connection.execute("CREATE INDEX idx_cards_user_id ON cards(user_id)")
    card_user_id = 999 if invalid_user else 1
    card_columns = [
        "id",
        "name",
        "type",
        "description",
        "is_multi_spec",
        "spec_name",
        "spec_value",
        "spec_name_2",
        "spec_value_2",
        "user_id",
    ]
    card_values = [
        41,
        "历史绑定卡券",
        "text",
        f"{ITEM_DELIVERY_BINDING_MARKER} legacy",
        1,
        "color",
        "red",
        "size",
        "small",
        card_user_id,
    ]
    if required_probe_guard:
        card_columns.append("probe_guard")
        card_values.append("existing")
    connection.execute(
        f"INSERT INTO cards ({', '.join(card_columns)}) "
        f"VALUES ({', '.join('?' for _ in card_values)})",
        card_values,
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


def test_current_cards_constraint_without_user_one_is_not_rebuilt(tmp_path):
    db_path, cards_rootpage = _make_current_cards_database_without_user_one(tmp_path)

    manager = DBManager(str(db_path))
    try:
        assert manager.conn.execute(
            "SELECT id FROM users ORDER BY id"
        ).fetchall() == [(2,)]
        assert manager.conn.execute(
            "SELECT spec_name_2, spec_value_2 FROM cards WHERE id = 51"
        ).fetchone() == ("size", "large")
        assert manager.conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE type = 'table' AND name = 'cards'"
        ).fetchone()[0] == cards_rootpage
        assert manager.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        manager.close()


def test_legacy_cards_constraint_migration_preserves_specs_and_references(tmp_path):
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
            "SELECT type, spec_name_2, spec_value_2 FROM cards WHERE id = 41"
        ).fetchone() == ("text", "size", "small")
        assert "yifan_api" in manager.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cards'"
        ).fetchone()[0]
        assert manager.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'cards_new'"
        ).fetchone() is None
        assert manager.conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name = 'idx_cards_user_id'"
        ).fetchone() == ("idx_cards_user_id",)
    finally:
        manager.close()


def test_misleading_yifan_api_check_is_migrated_instead_of_skipped(tmp_path):
    db_path = _make_legacy_cards_database(
        tmp_path,
        type_constraint="type <> 'yifan_api'",
    )

    manager = DBManager(str(db_path))
    try:
        manager.conn.execute(
            "INSERT INTO cards (name, type, user_id) VALUES (?, ?, ?)",
            ("真实探测卡券", "yifan_api", 1),
        )
        assert manager.conn.execute(
            "SELECT type FROM cards WHERE name = ?",
            ("真实探测卡券",),
        ).fetchone() == ("yifan_api",)
    finally:
        manager.close()


def test_non_check_probe_error_fails_safely_without_rebuilding(tmp_path):
    db_path = _make_legacy_cards_database(
        tmp_path,
        type_constraint="type IN ('api', 'yifan_api', 'text', 'data', 'image')",
        required_probe_guard=True,
    )

    with pytest.raises(RuntimeError, match="无法安全探测cards表约束"):
        DBManager(str(db_path))

    connection = sqlite3.connect(str(db_path))
    try:
        assert connection.execute(
            "SELECT probe_guard FROM cards WHERE id = 41"
        ).fetchone() == ("existing",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'cards_new'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name = 'idx_cards_user_id'"
        ).fetchone() == ("idx_cards_user_id",)
    finally:
        connection.close()


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


def test_pre_migration_commit_database_error_aborts_initialization(
    tmp_path,
    monkeypatch,
):
    db_path = _make_legacy_cards_database(tmp_path)
    _inject_cards_migration_connection_fault(monkeypatch, "pre_migration_commit")

    with pytest.raises(
        _CardsTableMigrationError,
        match="提交cards表迁移前的事务失败",
    ) as exc_info:
        DBManager(str(db_path))

    assert isinstance(exc_info.value.__cause__, sqlite3.DatabaseError)
    assert "injected pre-migration commit failure" in str(exc_info.value.__cause__)


@pytest.mark.parametrize(
    ("fault", "injected_message"),
    [
        ("restore_foreign_keys", "injected foreign-key restore failure"),
        (
            "verify_restored_foreign_keys",
            "injected foreign-key verification failure",
        ),
    ],
)
def test_foreign_key_restore_database_error_aborts_initialization(
    tmp_path,
    monkeypatch,
    fault,
    injected_message,
):
    db_path = _make_legacy_cards_database(tmp_path)
    _inject_cards_migration_connection_fault(monkeypatch, fault)

    with pytest.raises(
        _CardsTableMigrationError,
        match="cards表迁移后恢复外键约束失败",
    ) as exc_info:
        DBManager(str(db_path))

    assert isinstance(exc_info.value.__cause__, sqlite3.DatabaseError)
    assert injected_message in str(exc_info.value.__cause__)


def test_rebuild_and_restore_failures_abort_and_log_restore_failure(
    tmp_path,
    monkeypatch,
):
    db_path = _make_legacy_cards_database(tmp_path, invalid_user=True)
    _inject_cards_migration_connection_fault(monkeypatch, "restore_foreign_keys")
    error_messages = []
    monkeypatch.setattr(
        db_manager.logger,
        "error",
        lambda message: error_messages.append(str(message)),
    )

    with pytest.raises(
        _CardsTableMigrationError,
        match="cards表约束迁移失败",
    ) as exc_info:
        DBManager(str(db_path))

    assert isinstance(exc_info.value.__cause__, sqlite3.IntegrityError)
    assert any(
        "cards表迁移后恢复外键约束失败" in message
        and "injected foreign-key restore failure" in message
        for message in error_messages
    )
