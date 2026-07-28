import json

from db_manager import DBManager


def test_get_item_info_logs_only_identifiers_and_field_summary(tmp_path, monkeypatch):
    manager = DBManager(str(tmp_path / "xianyu.sqlite3"))
    secret_link = "https://pan.example/s/private-token"
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
