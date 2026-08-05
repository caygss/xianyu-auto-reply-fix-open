from types import SimpleNamespace

import pytest

import reply_server
from guided_setup_service import build_guided_status


READY_RUNTIME = {
    "connection_state": "connected",
    "running": True,
    "message_stream_ready": True,
}


def test_guided_completion_walks_items_delivery_republish_then_ready():
    cases = [
        (
            {"item_count": 0, "delivery_configured": False, "republish_configured": False},
            "no_items",
            "go_to_item_management",
        ),
        (
            {
                "item_count": 1,
                "delivery_configured": False,
                "republish_configured": False,
                "target_item_id": "item-1",
            },
            "delivery_config",
            "go_to_delivery_config",
        ),
        (
            {
                "item_count": 1,
                "delivery_configured": True,
                "republish_configured": False,
                "target_item_id": "item-1",
            },
            "republish_config",
            "go_to_republish_config",
        ),
        (
            {
                "item_count": 1,
                "delivery_configured": True,
                "republish_configured": True,
                "target_item_id": "item-1",
            },
            "ready_to_wait_for_order",
            "finish",
        ),
    ]

    for summary, expected_step_id, expected_action in cases:
        status = build_guided_status(READY_RUNTIME, delivery_summary=summary)
        assert status["step_id"] == expected_step_id
        assert status["primary_action"] == expected_action


def test_paused_or_disabled_republish_template_is_not_configured(monkeypatch):
    monkeypatch.setattr(
        reply_server.db_manager,
        "get_items_by_cookie",
        lambda cookie_id: [{"item_id": "item-1", "item_title": "安全标题"}],
    )
    monkeypatch.setattr(reply_server.db_manager, "get_cookie_details", lambda cookie_id: {"user_id": 7})
    monkeypatch.setattr(
        reply_server,
        "_get_republish_store",
        lambda: SimpleNamespace(
            list_templates=lambda cookie_id: [
                SimpleNamespace(
                    current_item_id="item-1",
                    title="安全标题",
                    auto_delivery=True,
                    delivery_choice="fixed_link",
                    delivery_content="不要返回的交付内容",
                    sku_delivery={},
                    auto_republish=True,
                    paused=True,
                ),
                SimpleNamespace(
                    current_item_id="item-1",
                    auto_delivery=True,
                    delivery_choice="fixed_link",
                    delivery_content="不要返回的交付内容",
                    sku_delivery={},
                    auto_republish=False,
                    paused=False,
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        reply_server,
        "DeliveryConfigService",
        lambda db: SimpleNamespace(count_for_account=lambda user_id, account_id: 0),
    )

    summary = reply_server._get_guided_delivery_summary("account-1")

    assert summary["item_count"] == 1
    assert summary["delivery_configured"] is True
    assert summary["republish_configured"] is False
    assert summary["target_item_id"] == "item-1"
    assert "不要返回的交付内容" not in repr(summary)


@pytest.mark.parametrize(
    "runtime_status",
    [
        {},
        {"connection_state": "connected"},
        {"connection_state": "connected", "message_stream_ready": False},
        {"connection_state": "reconnecting", "running": True, "message_stream_ready": True},
    ],
)
def test_runtime_not_ready_always_precedes_delivery_tasks(runtime_status):
    status = build_guided_status(
        runtime_status,
        delivery_summary={
            "item_count": 1,
            "delivery_configured": True,
            "republish_configured": True,
        },
    )

    assert status["primary_action"] not in {
        "go_to_item_management",
        "go_to_delivery_config",
        "go_to_republish_config",
        "finish",
    }
