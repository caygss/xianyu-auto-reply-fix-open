import asyncio
import pytest


class RecordingCoordinator:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def on_delivery_finalized(self, order_id, cookie_id, item_id, order_context):
        if self.error:
            raise self.error
        self.calls.append((order_id, cookie_id, item_id, order_context))


def _bare_live(coordinator=None):
    from XianyuAutoAsync import XianyuLive

    live = XianyuLive.__new__(XianyuLive)
    live.cookie_id = "account-1"
    live.republish_coordinator = coordinator
    live.delivery_sent_orders = set()
    live.last_delivery_time = {}
    live.order_status_handler = None
    return live


def test_delivery_republish_hook_only_runs_after_new_shipped_persistence(monkeypatch):
    coordinator = RecordingCoordinator()
    live = _bare_live(coordinator)
    monkeypatch.setattr(
        "XianyuAutoAsync.db_manager.get_order_by_id",
        lambda order_id: {"order_id": order_id, "order_status": "shipped"},
    )

    live._notify_republish_after_delivery_finalized(
        order_id="order-1",
        item_id="item-1",
        previous_status="pending_ship",
        persisted_status="shipped",
        order_context={"sku_id": "sku-1"},
    )
    live._notify_republish_after_delivery_finalized(
        order_id="order-1",
        item_id="item-1",
        previous_status="shipped",
        persisted_status="shipped",
        order_context={"sku_id": "sku-1"},
    )

    assert len(coordinator.calls) == 1


def test_delivery_success_sync_calls_hook_after_shipped_order_write(monkeypatch):
    live = _bare_live()
    calls = []
    statuses = iter(
        [
            {"order_id": "order-1", "order_status": "pending_ship"},
            {"order_id": "order-1", "order_status": "shipped", "item_id": "item-1"},
        ]
    )
    monkeypatch.setattr(
        "XianyuAutoAsync.db_manager.get_order_by_id", lambda order_id: next(statuses)
    )
    monkeypatch.setattr(
        "XianyuAutoAsync.db_manager.insert_or_update_order", lambda **kwargs: True
    )
    monkeypatch.setattr(
        live,
        "_summarize_delivery_progress",
        lambda order_id, expected_quantity=1: {"aggregate_status": "shipped"},
    )
    monkeypatch.setattr(
        live,
        "_notify_republish_after_delivery_finalized",
        lambda **kwargs: calls.append(kwargs),
    )

    live._sync_order_delivery_progress("order-1", "account-1")

    assert calls == [
        {
            "order_id": "order-1",
            "item_id": "item-1",
            "previous_status": "pending_ship",
            "persisted_status": "shipped",
        }
    ]


@pytest.mark.parametrize(
    "previous_status,persisted_status",
    [
        ("pending_ship", "pending_ship"),
        ("pending_ship", "refunding"),
        ("pending_ship", "cancelled"),
        ("refunding", "shipped"),
        ("error", "shipped"),
        ("shipped", "shipped"),
    ],
)
def test_delivery_republish_hook_skips_failed_refund_and_duplicate_states(
    previous_status, persisted_status, monkeypatch
):
    coordinator = RecordingCoordinator()
    live = _bare_live(coordinator)
    monkeypatch.setattr(
        "XianyuAutoAsync.db_manager.get_order_by_id",
        lambda order_id: {"order_id": order_id, "order_status": persisted_status},
    )

    live._notify_republish_after_delivery_finalized(
        order_id="order-1",
        item_id="item-1",
        previous_status=previous_status,
        persisted_status=persisted_status,
        order_context={"sku_id": "sku-1"},
    )

    assert coordinator.calls == []


def test_coordinator_exception_does_not_escape_delivery_success(monkeypatch):
    coordinator = RecordingCoordinator(RuntimeError("private coordinator detail"))
    live = _bare_live(coordinator)
    monkeypatch.setattr(
        "XianyuAutoAsync.db_manager.get_order_by_id",
        lambda order_id: {"order_id": order_id, "order_status": "shipped"},
    )
    captured = []
    monkeypatch.setattr(
        "XianyuAutoAsync.logger.warning", lambda message, *args, **kwargs: captured.append(message)
    )

    result = live._notify_republish_after_delivery_finalized(
        order_id="order-1",
        item_id="item-1",
        previous_status="pending_ship",
        persisted_status="shipped",
        order_context={"sku_id": "sku-1"},
    )

    assert result is None
    assert captured == ["delivery_republish_hook_failed"]


def test_republish_order_context_contains_only_sku_fields_and_never_logs_secrets(monkeypatch):
    from XianyuAutoAsync import XianyuLive

    raw_order = {
        "sku_id": "sku-1",
        "sku_name": "blue",
        "spec_value": "ignored-non-sku-field",
        "cookie": "COOKIE-SHOULD-NOT-LEAK",
        "token": "TOKEN-SHOULD-NOT-LEAK",
        "buyer_id": "buyer-1",
        "nested": {"skuId": "ignored-unknown-nesting"},
        "sku_info": {"spec_id": "spec-1", "delivery_content": "SECRET-LINK"},
    }
    live = _bare_live()
    monkeypatch.setattr("XianyuAutoAsync.db_manager.get_order_by_id", lambda order_id: raw_order)

    context = live._build_republish_order_context("order-1")

    assert context == {
        "sku_id": "sku-1",
        "sku_name": "blue",
        "sku_info": {"spec_id": "spec-1"},
    }
    assert "COOKIE-SHOULD-NOT-LEAK" not in repr(context)
    assert "TOKEN-SHOULD-NOT-LEAK" not in repr(context)
    assert "SECRET-LINK" not in repr(context)


def test_load_republish_config_uses_safe_defaults_and_parses_scalars():
    from Start import load_republish_config

    assert load_republish_config({}) == {
        "enabled": False,
        "dry_run": True,
        "check_interval_seconds": 30.0,
        "delay_seconds": 300.0,
        "max_retries": 3,
        "retry_backoff_seconds": (300.0, 900.0, 1800.0),
        "account_id": "",
    }
    assert load_republish_config(
        {
            "enabled": "true",
            "dry_run": "false",
            "check_interval_seconds": "12.5",
            "delay_seconds": "60",
            "max_retries": "4",
            "retry_backoff_seconds": ["1", 2, 3.5],
            "account_id": "account-1",
        }
    ) == {
        "enabled": True,
        "dry_run": False,
        "check_interval_seconds": 12.5,
        "delay_seconds": 60.0,
        "max_retries": 4,
        "retry_backoff_seconds": (1.0, 2.0, 3.5),
        "account_id": "account-1",
    }


def test_republish_availability_adapter_maps_listing_states():
    from Start import XianyuItemAvailabilityAdapter
    from republish_service import ItemAvailability

    class Live:
        async def get_all_items(self):
            return {"success": True, "items": [{"itemId": "item-1"}]}

    adapter = XianyuItemAvailabilityAdapter(Live())
    assert asyncio.run(adapter.check("account-1", "item-1")) is ItemAvailability.AVAILABLE
    assert asyncio.run(adapter.check("account-1", "item-2")) is ItemAvailability.UNAVAILABLE


def test_republish_availability_accepts_items_nested_under_successful_data():
    from Start import XianyuItemAvailabilityAdapter
    from republish_service import ItemAvailability

    class Live:
        async def get_all_items(self):
            return {"success": True, "data": {"items": [{"itemId": "item-1"}]}}

    adapter = XianyuItemAvailabilityAdapter(Live())
    assert asyncio.run(adapter.check("account-1", "item-1")) is ItemAvailability.AVAILABLE


def test_republish_availability_adapter_returns_unknown_on_listing_error():
    from Start import XianyuItemAvailabilityAdapter
    from republish_service import ItemAvailability

    class Live:
        async def get_item_list_info(self):
            raise RuntimeError("listing unavailable")

    adapter = XianyuItemAvailabilityAdapter(Live())
    assert asyncio.run(adapter.check("account-1", "item-1")) is ItemAvailability.UNKNOWN


@pytest.mark.parametrize(
    "result",
    [
        {"success": False, "items": [{"itemId": "item-1"}]},
        {"error": "opaque failure", "items": [{"itemId": "item-1"}]},
        {"items": [{"itemId": "item-1"}]},
    ],
)
def test_republish_availability_requires_explicit_success(result):
    from Start import XianyuItemAvailabilityAdapter
    from republish_service import ItemAvailability

    class Live:
        async def get_all_items(self):
            return result

    adapter = XianyuItemAvailabilityAdapter(Live())
    assert asyncio.run(adapter.check("account-1", "item-1")) is ItemAvailability.UNKNOWN


def test_republish_availability_prefers_safe_active_id_query(monkeypatch):
    from Start import XianyuItemAvailabilityAdapter
    from republish_service import ItemAvailability

    calls = []

    class Live:
        async def get_active_item_ids_for_republish(self):
            calls.append("safe")
            return {"item-1", "item-2"}

        async def get_all_items(self):
            calls.append("details")
            raise AssertionError("detail listing must not be called")

    adapter = XianyuItemAvailabilityAdapter(Live())
    assert asyncio.run(adapter.check("account-1", "item-1")) is ItemAvailability.AVAILABLE
    assert asyncio.run(adapter.check("account-1", "item-3")) is ItemAvailability.UNAVAILABLE
    assert calls == ["safe", "safe"]


def test_republish_availability_does_not_log_raw_listing_response(monkeypatch):
    from Start import XianyuItemAvailabilityAdapter
    from republish_service import ItemAvailability

    emitted = []

    class Live:
        async def get_all_items(self):
            return {"success": False, "error": "private-response-marker"}

    monkeypatch.setattr("Start.logger.warning", lambda *args, **kwargs: emitted.append(args))
    monkeypatch.setattr("Start.logger.error", lambda *args, **kwargs: emitted.append(args))
    adapter = XianyuItemAvailabilityAdapter(Live())
    assert asyncio.run(adapter.check("account-1", "item-1")) is ItemAvailability.UNKNOWN
    assert emitted == []


def test_live_republish_id_query_returns_only_safe_ids(monkeypatch):
    from XianyuAutoAsync import XianyuLive

    live = XianyuLive.__new__(XianyuLive)
    calls = []

    async def quiet_listing(*args, **kwargs):
        calls.append(kwargs)
        return {"success": True, "item_ids": {"item-1", "item-2"}}

    monkeypatch.setattr(live, "get_item_list_info", quiet_listing)
    assert asyncio.run(live.get_active_item_ids_for_republish()) == {"item-1", "item-2"}
    assert calls == [{"page_number": 1, "page_size": 100, "quiet": True}]


def test_live_republish_id_query_hides_underlying_failure(monkeypatch):
    from XianyuAutoAsync import XianyuLive

    live = XianyuLive.__new__(XianyuLive)

    async def quiet_listing(*args, **kwargs):
        raise RuntimeError("raw-response-marker")

    monkeypatch.setattr(live, "get_item_list_info", quiet_listing)
    with pytest.raises(RuntimeError, match="^republish_active_item_query_failed$"):
        asyncio.run(live.get_active_item_ids_for_republish())


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"cardList": None},
        {"cardList": {"unexpected": True}},
        {"cardList": ["unexpected-card"]},
        {"cardList": [{"cardData": None}]},
        {"cardList": [{"cardData": {}}]},
    ],
)
def test_quiet_item_listing_rejects_invalid_card_list(data):
    from XianyuAutoAsync import XianyuLive

    class Response:
        headers = {}

        async def json(self):
            return {"ret": ["SUCCESS::调用成功"], "data": data}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Session:
        def post(self, *args, **kwargs):
            return Response()

    live = XianyuLive.__new__(XianyuLive)
    live.session = Session()
    live.cookies_str = "session=opaque"
    live.myid = ""
    live._apply_response_cookie_updates = lambda *args, **kwargs: asyncio.sleep(0, result=False)

    result = asyncio.run(live.get_item_list_info(quiet=True))
    assert result == {
        "success": False,
        "error": "republish_active_item_query_failed",
    }


def test_quiet_item_listing_accepts_only_a_valid_empty_card_list():
    from XianyuAutoAsync import XianyuLive

    class Response:
        headers = {}

        async def json(self):
            return {"ret": ["SUCCESS::调用成功"], "data": {"cardList": []}}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Session:
        def post(self, *args, **kwargs):
            return Response()

    live = XianyuLive.__new__(XianyuLive)
    live.session = Session()
    live.cookies_str = "session=opaque"
    live.myid = ""
    live._apply_response_cookie_updates = lambda *args, **kwargs: asyncio.sleep(0, result=False)

    assert asyncio.run(live.get_item_list_info(quiet=True)) == {
        "success": True,
        "item_ids": set(),
    }


def test_build_republish_runtime_wires_one_live_account_and_is_idempotent(monkeypatch):
    import Start

    class Store:
        def __init__(self, path):
            self.path = path
            self.closed = 0

        def close(self):
            self.closed += 1

    class Publisher:
        def __init__(self, cookie, cookie_id):
            self.cookie = cookie
            self.cookie_id = cookie_id
            self.closed = 0

        async def close_session(self):
            self.closed += 1

    class Coordinator:
        def __init__(self, store, publisher, availability, **kwargs):
            self.store = store
            self.publisher = publisher
            self.availability = availability
            self.options = kwargs

    class Scheduler:
        def __init__(self, coordinator, interval):
            self.coordinator = coordinator
            self.interval = interval
            self.starts = 0
            self.stops = 0

        async def start(self):
            self.starts += 1

        async def stop(self):
            self.stops += 1

    class Live:
        def __init__(self):
            self.coordinator = None
            self.coordinator_calls = []

        def set_republish_coordinator(self, coordinator):
            self.coordinator_calls.append(coordinator)
            self.coordinator = coordinator

    live = Live()
    manager = type(
        "Manager",
        (),
        {
            "cookies": {"account-1": "opaque-cookie"},
            "live_instances": {"account-1": live},
            "get_enabled_cookies": lambda self: self.cookies,
        },
    )()
    monkeypatch.setattr(Start, "RepublishStore", Store)
    monkeypatch.setattr(Start, "ItemPublisher", Publisher)
    monkeypatch.setattr(Start, "RepublishCoordinator", Coordinator)

    settings = Start.load_republish_config(
        {
            "enabled": True,
            "dry_run": True,
            "check_interval_seconds": 7,
            "retry_backoff_seconds": [4, 5, 6],
            "account_id": "account-1",
        }
    )
    runtime = Start.build_republish_runtime(
        manager, settings, scheduler_cls=Scheduler, store_path="test-republish.db"
    )
    assert runtime is not None
    assert live.coordinator is runtime.coordinator
    assert runtime.scheduler.interval == 7.0
    assert runtime.coordinator.options["dry_run"] is True
    assert runtime.coordinator.options["retry_backoff_seconds"] == (4.0, 5.0, 6.0)

    async def exercise():
        await runtime.start()
        await runtime.start()
        await runtime.stop()
        await runtime.stop()

    asyncio.run(exercise())
    assert runtime.scheduler.starts == 1
    assert runtime.scheduler.stops == 1
    assert runtime.publisher.closed == 1
    assert runtime.store.closed == 1
    assert live.coordinator is None
    assert live.coordinator_calls[-1] is None


def test_build_republish_runtime_detaches_when_construction_fails(monkeypatch):
    import Start

    class Store:
        def __init__(self, path):
            self.closed = 0

        def close(self):
            self.closed += 1

    class Publisher:
        def __init__(self, cookie, cookie_id):
            pass

        async def close_session(self):
            pass

    class Coordinator:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("construction failed")

    class Live:
        def __init__(self):
            self.coordinator_calls = []

        def set_republish_coordinator(self, coordinator):
            self.coordinator_calls.append(coordinator)

    live = Live()
    manager = type(
        "Manager",
        (),
        {
            "cookies": {"account-1": "opaque-cookie"},
            "live_instances": {"account-1": live},
            "get_enabled_cookies": lambda self: self.cookies,
        },
    )()
    monkeypatch.setattr(Start, "RepublishStore", Store)
    monkeypatch.setattr(Start, "ItemPublisher", Publisher)
    monkeypatch.setattr(Start, "RepublishCoordinator", Coordinator)

    with pytest.raises(RuntimeError, match="construction failed"):
        Start.build_republish_runtime(
            manager,
            Start.load_republish_config({"enabled": True}),
            scheduler_cls=lambda *args, **kwargs: None,
            store_path="test-republish.db",
        )
    assert live.coordinator_calls == [None]


def test_republish_runtime_detaches_even_when_scheduler_stop_fails():
    import Start

    class Scheduler:
        async def stop(self):
            raise RuntimeError("stop failed")

    class Publisher:
        async def close_session(self):
            pass

    class Store:
        def close(self):
            pass

    class Live:
        def __init__(self):
            self.coordinator_calls = []

        def set_republish_coordinator(self, coordinator):
            self.coordinator_calls.append(coordinator)

    live = Live()
    runtime = Start._RepublishRuntime(
        Scheduler(), object(), Publisher(), Store(), live
    )
    with pytest.raises(RuntimeError, match="stop failed"):
        asyncio.run(runtime.stop())
    assert live.coordinator_calls == [None]
