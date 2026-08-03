import json
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest
from loguru import logger

from card_inventory_service import CardInventoryService
from db_manager import DBManager
from delivery_config_service import DeliveryConfigError, DeliveryConfigService
from delivery_adapter_service import (
    DeliveryDispatchError,
    DeliveryDispatcher,
    DeliveryRequest,
    ProviderResponse,
)


class FakeTransport:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def request(self, method, url, headers, json_body, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json_body": json_body,
                "timeout": timeout,
            }
        )
        if self.error:
            raise self.error
        if not self.responses:
            raise AssertionError("fake transport ran out of responses")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture
def services(tmp_path):
    manager = DBManager(str(tmp_path / "delivery.sqlite3"))
    configs = DeliveryConfigService(manager)
    inventory = CardInventoryService(manager)
    yield manager, configs, inventory
    manager.close()


def request(**overrides):
    values = {
        "user_id": 1,
        "card_id": 7,
        "account_id": "account-a",
        "order_id": "order-1",
        "reservation_id": None,
        "context": {"order_id": "order-1", "buyer_id": "buyer-1", "item_id": "item-7"},
    }
    values.update(overrides)
    return DeliveryRequest(**values)


def test_fixed_link_dispatch_returns_configured_url(services):
    _, configs, inventory = services
    configs.save(1, 7, "account-a", "fixed_link", {"url": "https://example.test/download"})

    result = DeliveryDispatcher(configs, inventory).prepare(request())

    assert result == {
        "mode": "fixed_link",
        "content": "https://example.test/download",
        "content_type": "text",
    }


@pytest.mark.parametrize("mode,secret", [("imported_card", "imported-secret"), ("generated_card", "generated-secret")])
def test_card_modes_read_only_the_scoped_reserved_card(services, mode, secret):
    _, configs, inventory = services
    inventory.save_settings(7, 1, "account-a", stock_ceiling=2)
    inventory.import_items(7, 1, "account-a", [secret])
    reservation = inventory.reserve_items(7, 1, "account-a", "order-1", 1)
    configs.save(1, 7, "account-a", mode, {"label": mode})

    result = DeliveryDispatcher(configs, inventory).prepare(
        request(reservation_id=reservation["reservation_id"])
    )

    assert result["mode"] == mode
    assert result["content"] == secret
    assert result["content_type"] == "text"
    assert inventory.get_inventory_summary(7, 1, "account-a")["sent"] == 1


def test_card_dispatch_rejects_reservation_scope_mismatch(services):
    _, configs, inventory = services
    inventory.save_settings(7, 1, "account-a", stock_ceiling=2)
    inventory.import_items(7, 1, "account-a", ["scoped-secret"])
    reservation = inventory.reserve_items(7, 1, "account-a", "order-1", 1)
    configs.save(1, 7, "account-b", "imported_card", {"label": "card"})

    with pytest.raises(DeliveryDispatchError) as error:
        DeliveryDispatcher(configs, inventory).prepare(
            request(account_id="account-b", reservation_id=reservation["reservation_id"])
        )

    assert error.value.code == "scope_mismatch"
    assert error.value.technical_category == "inventory"
    assert "作用域" in str(error.value)


def test_dispatcher_rejects_unknown_mode_before_loading_config(services):
    _, configs, inventory = services

    with pytest.raises(DeliveryDispatchError) as error:
        DeliveryDispatcher(configs, inventory).prepare(request(mode="unknown"))

    assert error.value.code == "invalid_mode"
    assert error.value.technical_category == "validation"


def test_dispatcher_reports_missing_config_in_chinese(services):
    _, configs, inventory = services

    with pytest.raises(DeliveryDispatchError) as error:
        DeliveryDispatcher(configs, inventory).prepare(request())

    assert error.value.code == "config_not_found"
    assert error.value.technical_category == "configuration"
    assert "交付配置" in str(error.value)


def test_provider_api_maps_request_fields_and_extracts_response_content(services):
    _, configs, inventory = services
    configs.save(
        1,
        7,
        "account-a",
        "provider_api",
        {
            "endpoint": "https://provider.test/issue",
            "token": "secret-token",
            "headers": {"X-Client": "client-1"},
            "field_mapping": {"order_id": "orderId", "buyer_id": "buyerId"},
            "response_field": "data.content",
            "timeout_seconds": 5,
            "max_retries": 0,
        },
    )
    transport = FakeTransport(
        [ProviderResponse(200, {}, json.dumps({"data": {"content": "provider-content"}}).encode())]
    )

    result = DeliveryDispatcher(configs, inventory, transport=transport).prepare(request())

    assert result == {
        "mode": "provider_api",
        "content": "provider-content",
        "content_type": "text",
    }
    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["json_body"] == {"orderId": "order-1", "buyerId": "buyer-1"}
    assert transport.calls[0]["headers"]["X-Client"] == "client-1"
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer secret-token"
    assert transport.calls[0]["headers"]["Content-Type"] == "application/json"
    assert transport.calls[0]["timeout"] == 5

    public_config = configs.get(1, 7, "account-a")
    assert "token" not in json.dumps(public_config, ensure_ascii=False)
    assert "secret-token" not in json.dumps(public_config, ensure_ascii=False)


@pytest.mark.parametrize(
    "config,match",
    [
        ({"endpoint": "ftp://provider.test/issue", "token": "t"}, "HTTP 或 HTTPS"),
        ({"endpoint": "https://provider.test/issue", "token": "t", "timeout_seconds": 0}, "超时"),
        ({"endpoint": "https://provider.test/issue", "token": "t", "max_retries": 4}, "重试"),
    ],
)
def test_provider_config_rejects_unsafe_network_bounds(services, config, match):
    _, configs, _ = services

    with pytest.raises(DeliveryConfigError, match=match):
        configs.save(1, 7, "account-a", "provider_api", config)


def test_provider_retries_transient_5xx_but_not_4xx(services):
    _, configs, inventory = services
    configs.save(
        1,
        7,
        "account-a",
        "provider_api",
        {"endpoint": "https://provider.test/issue", "token": "t", "max_retries": 1},
    )
    transport = FakeTransport(
        [
            ProviderResponse(503, {}, b'{"error":"busy"}'),
            ProviderResponse(200, {}, b'{"content":"ok"}'),
        ]
    )

    result = DeliveryDispatcher(configs, inventory, transport=transport).prepare(request())

    assert result["content"] == "ok"
    assert len(transport.calls) == 2

    transport = FakeTransport([ProviderResponse(400, {}, b'{"error":"bad request"}'), ProviderResponse(200, {}, b'{"content":"not-used"}')])
    with pytest.raises(DeliveryDispatchError) as error:
        DeliveryDispatcher(configs, inventory, transport=transport).prepare(request())
    assert error.value.code == "provider_http_error"
    assert len(transport.calls) == 1


def test_provider_failure_is_readable_domain_error_and_does_not_log_token(services):
    _, configs, inventory = services
    configs.save(
        1,
        7,
        "account-a",
        "provider_api",
        {"endpoint": "https://provider.test/issue", "token": "secret-token", "max_retries": 0},
    )
    transport = FakeTransport(error=TimeoutError("upstream timed out"))
    sink = []
    sink_id = logger.add(lambda message: sink.append(str(message)), format="{message}")
    try:
        with pytest.raises(DeliveryDispatchError) as error:
            DeliveryDispatcher(configs, inventory, transport=transport).prepare(request())
    finally:
        logger.remove(sink_id)

    assert error.value.code == "provider_transport_error"
    assert error.value.technical_category == "provider_transport"
    assert "外部交付服务" in str(error.value)
    assert "secret-token" not in "".join(sink)


def test_provider_retries_oserror_from_transport(services):
    _, configs, inventory = services
    configs.save(
        1,
        7,
        "account-a",
        "provider_api",
        {"endpoint": "https://provider.test/issue", "token": "t", "max_retries": 1},
    )
    transport = FakeTransport(
        [OSError("socket closed"), ProviderResponse(200, {}, b'{"content":"ok"}')]
    )

    result = DeliveryDispatcher(configs, inventory, transport=transport).prepare(request())

    assert result["content"] == "ok"
    assert len(transport.calls) == 2


def test_provider_rejects_oversized_response_without_returning_body(services):
    _, configs, inventory = services
    configs.save(
        1,
        7,
        "account-a",
        "provider_api",
        {"endpoint": "https://provider.test/issue", "token": "t", "max_retries": 0},
    )
    transport = FakeTransport([ProviderResponse(200, {}, b"x" * (64 * 1024 + 1))])

    with pytest.raises(DeliveryDispatchError) as error:
        DeliveryDispatcher(configs, inventory, transport=transport).prepare(request())

    assert error.value.code == "provider_response_too_large"
    assert len(str(error.value)) < 200


def test_provider_invalid_utf8_is_a_readable_domain_error(services):
    _, configs, inventory = services
    configs.save(
        1,
        7,
        "account-a",
        "provider_api",
        {"endpoint": "https://provider.test/issue", "token": "t", "max_retries": 0},
    )
    transport = FakeTransport([ProviderResponse(200, {}, b"\xff\xfe")])

    with pytest.raises(DeliveryDispatchError) as error:
        DeliveryDispatcher(configs, inventory, transport=transport).prepare(request())

    assert error.value.code == "provider_response_invalid"
    assert "JSON" in str(error.value)


def test_provider_non_response_transport_value_is_adapter_error_without_status_attribute_access(services):
    _, configs, inventory = services
    configs.save(
        1,
        7,
        "account-a",
        "provider_api",
        {"endpoint": "https://provider.test/issue", "token": "t", "max_retries": 3},
    )
    transport = FakeTransport([object(), ProviderResponse(200, {}, b'{"content":"unused"}')])

    with pytest.raises(DeliveryDispatchError) as error:
        DeliveryDispatcher(configs, inventory, transport=transport).prepare(request())

    assert error.value.code == "provider_response_invalid"
    assert error.value.technical_category == "provider_response"
    assert len(transport.calls) == 1


def test_provider_does_not_retry_programming_exception(services):
    _, configs, inventory = services
    configs.save(
        1,
        7,
        "account-a",
        "provider_api",
        {"endpoint": "https://provider.test/issue", "token": "t", "max_retries": 3},
    )
    transport = FakeTransport(
        [ValueError("programming failure"), ProviderResponse(200, {}, b'{"content":"unused"}')]
    )

    with pytest.raises(DeliveryDispatchError) as error:
        DeliveryDispatcher(configs, inventory, transport=transport).prepare(request())

    assert error.value.code == "provider_transport_error"
    assert len(transport.calls) == 1


def test_provider_retries_network_exception_up_to_max_retries(services):
    _, configs, inventory = services
    configs.save(
        1,
        7,
        "account-a",
        "provider_api",
        {"endpoint": "https://provider.test/issue", "token": "t", "max_retries": 3},
    )
    transport = FakeTransport(
        [
            URLError("offline"),
            URLError("offline"),
            URLError("offline"),
            ProviderResponse(200, {}, b'{"content":"recovered"}'),
        ]
    )

    result = DeliveryDispatcher(configs, inventory, transport=transport).prepare(request())

    assert result["content"] == "recovered"
    assert len(transport.calls) == 4


def test_provider_max_retries_three_caps_retryable_http_to_four_calls(services):
    _, configs, inventory = services
    configs.save(
        1,
        7,
        "account-a",
        "provider_api",
        {"endpoint": "https://provider.test/issue", "token": "t", "max_retries": 3},
    )
    transport = FakeTransport(
        [ProviderResponse(503, {}, b'{"error":"busy"}') for _ in range(4)]
    )

    with pytest.raises(DeliveryDispatchError) as error:
        DeliveryDispatcher(configs, inventory, transport=transport).prepare(request())

    assert error.value.code == "provider_http_error"
    assert len(transport.calls) == 4


@pytest.mark.parametrize("status_code", [400, 404])
def test_provider_http_error_other_4xx_does_not_retry(services, status_code):
    _, configs, inventory = services
    configs.save(
        1,
        7,
        "account-a",
        "provider_api",
        {"endpoint": "https://provider.test/issue", "token": "t", "max_retries": 3},
    )
    transport = FakeTransport(
        [
            HTTPError(
                "https://provider.test/issue",
                status_code,
                "client error",
                {},
                BytesIO(b'{"error":"client"}'),
            ),
            ProviderResponse(200, {}, b'{"content":"must-not-be-used"}'),
        ]
    )

    with pytest.raises(DeliveryDispatchError) as error:
        DeliveryDispatcher(configs, inventory, transport=transport).prepare(request())

    assert error.value.code == "provider_http_error"
    assert len(transport.calls) == 1


def test_xianyu_delivery_seam_reuses_existing_text_steps(monkeypatch):
    from XianyuAutoAsync import XianyuLive

    captured = {}

    class FakeDispatcher:
        def __init__(self, config_service, inventory_service, *, transport=None):
            captured["transport"] = transport

        def prepare(self, delivery_request):
            captured["request"] = delivery_request
            return {"mode": "fixed_link", "content": "https://example.test/item", "content_type": "text"}

    monkeypatch.setattr("delivery_adapter_service.DeliveryDispatcher", FakeDispatcher)
    live = XianyuLive.__new__(XianyuLive)
    live.user_id = 1
    live.cookie_id = "account-a"

    prepared = live._prepare_configured_delivery(
        card_id=7,
        order_id="order-1",
        buyer_id="buyer-1",
        reservation_id="reservation-1",
        item_id="item-7",
        provider_transport="fake-transport",
    )

    assert prepared == {
        "mode": "fixed_link",
        "content": "https://example.test/item",
        "content_type": "text",
    }
    assert captured["transport"] == "fake-transport"
    assert captured["request"].user_id == 1
    assert captured["request"].card_id == 7
    assert captured["request"].account_id == "account-a"
    assert captured["request"].reservation_id == "reservation-1"


def test_xianyu_delivery_seam_forwards_quantity_and_idempotency_context(monkeypatch):
    from XianyuAutoAsync import XianyuLive

    captured = {}

    class FakeDispatcher:
        def __init__(self, config_service, inventory_service, *, transport=None):
            pass

        def prepare(self, delivery_request):
            captured["request"] = delivery_request
            return {"mode": "fixed_link", "content": "https://example.test/item", "content_type": "text"}

    monkeypatch.setattr("delivery_adapter_service.DeliveryDispatcher", FakeDispatcher)
    live = XianyuLive.__new__(XianyuLive)
    live.user_id = 1
    live.cookie_id = "account-a"

    live._prepare_configured_delivery(
        card_id=7,
        order_id="order-1",
        buyer_id="buyer-1",
        reservation_id="reservation-1",
        item_id="item-7",
        quantity=3,
        order_line_id="line-1",
        idempotency_key="scope-key",
    )

    request = captured["request"]
    assert request.quantity == 3
    assert request.order_line_id == "line-1"
    assert request.idempotency_key == "scope-key"
    assert request.context["quantity"] == 3
    assert request.context["order_line_id"] == "line-1"
    assert request.context["idempotency_key"] == "scope-key"


def test_xianyu_configured_order_entry_uses_orchestration_service(monkeypatch):
    from XianyuAutoAsync import XianyuLive

    captured = {}

    class FakeInventory:
        def __init__(self, db):
            captured["inventory_db"] = db

    class FakeDispatcher:
        def __init__(self, config_service, inventory_service, *, transport=None):
            captured["dispatcher_dependencies"] = (config_service, inventory_service, transport)

    class FakeService:
        def __init__(self, db, inventory, dispatcher):
            captured["service_dependencies"] = (db, inventory, dispatcher)

        def orchestrate(self, request, sender):
            captured["request"] = request
            captured["sender"] = sender
            return {"status": "sent"}

    monkeypatch.setattr("card_inventory_service.CardInventoryService", FakeInventory)
    monkeypatch.setattr("delivery_adapter_service.DeliveryDispatcher", FakeDispatcher)
    monkeypatch.setattr("delivery_orchestration_service.DeliveryOrchestrationService", FakeService)
    live = XianyuLive.__new__(XianyuLive)
    live.user_id = 1
    live.cookie_id = "account-a"
    sender = lambda contents, request: True

    result = live._orchestrate_configured_delivery(
        card_id=7,
        order_id="order-1",
        buyer_id="buyer-1",
        item_id="item-7",
        quantity="3",
        order_line_id=None,
        delivery_config={"mode": "fixed_link", "url": "https://example.test/item"},
        sender=sender,
    )

    assert result == {"status": "sent"}
    request = captured["request"]
    assert request.user_id == 1
    assert request.account_id == "account-a"
    assert request.quantity == "3"
    assert request.order_line_id is None
    assert request.context["buyer_id"] == "buyer-1"
    assert captured["sender"] is sender


def test_xianyu_auto_delivery_routes_reserved_content_through_delivery_seam(monkeypatch):
    from XianyuAutoAsync import XianyuLive

    captured = {}
    live = XianyuLive.__new__(XianyuLive)
    live.user_id = 1
    live.cookie_id = "account-a"
    live.myid = "buyer-1"

    async def fetch_order_detail_info(*args, **kwargs):
        return None

    def prepare_configured_delivery(**kwargs):
        captured.update(kwargs)
        return {"mode": "imported_card", "content": "reserved-secret", "content_type": "text"}

    monkeypatch.setattr(live, "fetch_order_detail_info", fetch_order_detail_info)
    monkeypatch.setattr(live, "_prepare_configured_delivery", prepare_configured_delivery)
    monkeypatch.setattr("XianyuAutoAsync.db_manager.get_item_info", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "XianyuAutoAsync.db_manager.get_item_multi_spec_status",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "XianyuAutoAsync.db_manager.get_delivery_rules_by_keyword",
        lambda *args, **kwargs: [
            {
                "id": 11,
                "keyword": "item title",
                "card_name": "card",
                "card_type": "text",
                "card_id": 7,
                "text_content": "legacy-content",
                "card_description": "",
            }
        ],
    )

    import asyncio

    result = asyncio.run(
        live._auto_delivery(
            "item-7",
            "item title",
            "order-1",
            "buyer-1",
            include_meta=True,
            delivery_reservation_id="reservation-1",
        )
    )

    assert result["content"] == "reserved-secret"
    assert captured == {
        "card_id": 7,
        "order_id": "order-1",
        "buyer_id": "buyer-1",
        "reservation_id": "reservation-1",
        "item_id": "item-7",
    }


def test_provider_adapter_forwards_quantity_and_idempotency_key(services):
    _, configs, inventory = services
    configs.save(
        1,
        7,
        "account-a",
        "provider_api",
        {"endpoint": "https://provider.test/issue", "token": "t"},
    )
    transport = FakeTransport(
        [ProviderResponse(200, {}, b'{"content":"provider-content"}')]
    )

    result = DeliveryDispatcher(configs, inventory, transport=transport).prepare(
        request(
            quantity=3,
            idempotency_key="scope-key",
            context={"order_id": "order-1"},
        )
    )

    assert result["content"] == "provider-content"
    assert transport.calls[0]["json_body"]["quantity"] == 3
    assert transport.calls[0]["json_body"]["idempotency_key"] == "scope-key"


def test_provider_adapter_overrides_untrusted_quantity_and_idempotency_key(services):
    _, configs, inventory = services
    configs.save(
        1,
        7,
        "account-a",
        "provider_api",
        {
            "endpoint": "https://provider.test/issue",
            "token": "t",
            "request_body": {"quantity": 99, "idempotency_key": "template-key"},
        },
    )
    transport = FakeTransport(
        [ProviderResponse(200, {}, b'{"content":"provider-content"}')]
    )

    DeliveryDispatcher(configs, inventory, transport=transport).prepare(
        request(
            quantity=3,
            idempotency_key="scope-key",
            context={"quantity": 88, "idempotency_key": "context-key"},
        )
    )

    assert transport.calls[0]["json_body"]["quantity"] == 3
    assert transport.calls[0]["json_body"]["idempotency_key"] == "scope-key"
