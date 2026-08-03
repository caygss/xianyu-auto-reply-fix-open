import asyncio
import io
import json
import sqlite3

import pytest
from fastapi import HTTPException, UploadFile

import reply_server
from db_manager import DBManager


def _record_foreign_keys_when_connections_close(monkeypatch):
    real_connect = sqlite3.connect
    foreign_key_states = []

    class TrackingConnection:
        def __init__(self, connection):
            self._connection = connection

        def cursor(self, *args, **kwargs):
            return self._connection.cursor(*args, **kwargs)

        def close(self):
            foreign_key_states.append(
                self._connection.execute("PRAGMA foreign_keys").fetchone()[0]
            )
            self._connection.close()

        def __getattr__(self, name):
            return getattr(self._connection, name)

    def tracked_connect(*args, **kwargs):
        return TrackingConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    return foreign_key_states


def test_get_item_info_logs_only_identifiers_and_field_summary(tmp_path, monkeypatch):
    manager = DBManager(str(tmp_path / "xianyu.sqlite3"))
    secret_link = "https://pan.example/s/private-token"
    assert manager.save_cookie("cookie-1", "cookie-value", user_id=1)
    assert manager.save_item_info(
        "cookie-1",
        "item-1",
        {
            "title": "网盘商品",
            "description": f"下载地址：{secret_link}",
            "images": ["https://img.example/item.jpg"],
        },
    )

    messages = []
    monkeypatch.setattr("db_manager.logger.info", messages.append)

    result = manager.get_item_info("cookie-1", "item-1")

    assert result is not None
    assert json.loads(result["item_detail"])["description"] == f"下载地址：{secret_link}"
    assert messages
    assert all(secret_link not in message for message in messages)
    assert any("cookie-1" in message and "item-1" in message for message in messages)
    assert any("field_count" in message for message in messages)


def test_qr_login_grace_deadline_round_trips_float_precision(tmp_path):
    manager = DBManager(str(tmp_path / "qr-grace.sqlite3"))
    assert manager.save_cookie("cookie-1", "cookie-value", user_id=1)

    assert manager.set_cookie_qr_login_grace_until("cookie-1", 1600.9)

    details = manager.get_cookie_details("cookie-1")
    assert details["qr_login_grace_until"] == 1600.9


def test_debug_keywords_connection_enables_foreign_keys(tmp_path, monkeypatch):
    manager = DBManager(str(tmp_path / "debug.sqlite3"))
    monkeypatch.setattr(reply_server, "db_manager", manager)
    foreign_key_states = _record_foreign_keys_when_connections_close(monkeypatch)

    try:
        result = reply_server.debug_keywords_table_info(
            {"user_id": 1, "username": "admin"}
        )
    finally:
        manager.close()

    assert result["table_columns"]
    assert foreign_key_states == [1]


def test_backup_validation_connection_enables_foreign_keys(tmp_path, monkeypatch):
    backup_path = tmp_path / "candidate.db"
    connection = sqlite3.connect(str(backup_path))
    connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    backup_content = backup_path.read_bytes()
    monkeypatch.chdir(tmp_path)
    foreign_key_states = _record_foreign_keys_when_connections_close(monkeypatch)
    upload = UploadFile(filename="candidate.db", file=io.BytesIO(backup_content))

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            reply_server.upload_database_backup(
                {"user_id": 1, "username": "admin"}, upload
            )
        )

    assert error.value.status_code == 400
    assert foreign_key_states == [1]
