import asyncio
import json
import math

import pytest

from republish_models import RepublishTemplate
from republish_service import (
    AvailabilityStatus,
    ItemPublisherAdapter,
    ItemAvailability,
    ManualActionRequired,
    RepublishConfigurationError,
    RepublishCoordinator,
)
from republish_store import RepublishStore
from republish_template_service import rotate_current_item_id


SHARED_LINK = "https://safe.example/shared"
SPECIAL_LINK = "https://safe.example/special"


class FakePublisher:
    def __init__(self, result="new-item-1", error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def publish(self, template):
        self.calls.append(template.template_id)
        if self.error:
            raise self.error
        return self.result


class FakeAvailability:
    def __init__(self, status):
        self.status = status
        self.calls = []

    async def check(self, cookie_id, item_id):
        self.calls.append((cookie_id, item_id))
        return self.status


def make_template(
    *,
    template_id="template-1",
    item_id="item-1",
    delivery_content=SHARED_LINK,
    sku_delivery=None,
    auto_delivery=True,
    auto_republish=True,
    paused=False,
):
    return RepublishTemplate(
        template_id=template_id,
        cookie_id="cookie-1",
        current_item_id=item_id,
        title="Reusable item",
        description="Description",
        images=["https://img.example/item.jpg"],
        delivery_choice="digital",
        delivery_content=delivery_content,
        sku_delivery=sku_delivery or {},
        auto_delivery=auto_delivery,
        auto_republish=auto_republish,
        paused=paused,
    )


def make_coordinator(store, publisher=None, availability=None, now=1000.0, logs=None, **kwargs):
    clock = kwargs.pop("clock", lambda: now)
    return RepublishCoordinator(
        store,
        publisher or FakePublisher(),
        availability or FakeAvailability(ItemAvailability.UNAVAILABLE),
        clock=clock,
        log_callback=(logs.append if logs is not None else None),
        **kwargs,
    )


@pytest.fixture
def store(tmp_path):
    return RepublishStore(tmp_path / "republish.sqlite3")


def test_delivery_finalized_enqueues_once_and_persists_order_context(store):
    template = make_template(sku_delivery={"sku-special": SPECIAL_LINK})
    store.upsert_template(template)
    coordinator = make_coordinator(store)
    context = {"sku_info": {"sku_id": "sku-special"}, "quantity": 1}

    first = coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", context)
    second = coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", context)

    assert first.status == second.status == "enqueued"
    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].order_context == context


def test_run_once_rejects_job_when_template_current_item_rotated_after_enqueue(store):
    store.upsert_template(make_template())
    coordinator = make_coordinator(store, publisher=FakePublisher(result="must-not-publish"))
    coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", {})
    rotate_current_item_id(store, "template-1", "item-2")

    result = asyncio.run(coordinator.run_once())

    assert result.status == "manual_required"
    assert result.reason == "template_item_snapshot_mismatch"
    assert store.list_jobs()[0].status == "manual_required"


def test_run_once_recovers_claim_when_template_read_raises(store, monkeypatch):
    store.upsert_template(make_template())
    logs = []
    coordinator = make_coordinator(store, logs=logs, retry_backoff_seconds=(0, 0, 0))
    coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", {})

    def fail_get_template(*args, **kwargs):
        raise RuntimeError("private database failure")

    monkeypatch.setattr(store, "get_template", fail_get_template)

    result = asyncio.run(coordinator.run_once())

    assert result.status == "retry"
    assert result.reason == "template_read_error:RuntimeError"
    assert store.list_jobs()[0].status == "retry"
    assert "private database failure" not in repr(logs)


def test_ordinary_value_error_from_publisher_is_retryable(store):
    store.upsert_template(make_template())
    publisher = FakePublisher(error=ValueError("temporary publish rejection"))
    coordinator = make_coordinator(store, publisher=publisher)
    coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", {})

    result = asyncio.run(coordinator.run_once())

    assert result.status == "retry"
    assert result.reason.startswith("publish_error:")
    assert store.list_jobs()[0].status == "retry"


def test_dry_run_obeys_three_retry_backoffs_then_manual(store):
    store.upsert_template(make_template())
    current_time = [1000.0]
    coordinator = make_coordinator(store, dry_run=True, clock=lambda: current_time[0])
    coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", {})

    outcomes = []
    due_times = []
    for _ in range(4):
        outcomes.append(asyncio.run(coordinator.run_once()))
        job = store.list_jobs()[0]
        due_times.append(job.available_at)
        if job.status == "retry":
            current_time[0] = job.available_at

    assert [outcome.status for outcome in outcomes] == [
        "preview",
        "preview",
        "preview",
        "manual_required",
    ]
    assert due_times[:3] == [1300.0, 2200.0, 4000.0]
    assert store.list_jobs()[0].attempts == 3


def test_restart_preserves_special_sku_context_and_does_not_cross_use_shared_link(tmp_path):
    db_path = tmp_path / "restart.sqlite3"
    store = RepublishStore(db_path)
    store.upsert_template(make_template(sku_delivery={"sku-special": SPECIAL_LINK}))
    coordinator = make_coordinator(store)
    coordinator.on_delivery_finalized(
        "order-1", "cookie-1", "item-1", {"sku_id": "sku-special"}
    )
    store.close()

    publisher = FakePublisher()
    with RepublishStore(db_path) as restarted:
        coordinator = make_coordinator(restarted, publisher=publisher)
        result = asyncio.run(coordinator.run_once())

    assert result.status == "succeeded"
    assert publisher.calls == ["template-1"]
    assert result.safe_delivery_summary != SHARED_LINK


@pytest.mark.parametrize("availability", [ItemAvailability.AVAILABLE, ItemAvailability.UNKNOWN])
def test_available_or_unknown_source_is_retried_without_publishing(store, availability):
    store.upsert_template(make_template())
    publisher = FakePublisher()
    coordinator = make_coordinator(
        store, publisher=publisher, availability=FakeAvailability(availability)
    )
    coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", {})

    result = asyncio.run(coordinator.run_once())

    assert result.status == "retry"
    assert publisher.calls == []
    assert store.list_jobs()[0].status == "retry"


def test_unavailable_source_is_published_and_new_id_is_persisted(store):
    store.upsert_template(make_template())
    publisher = FakePublisher(result="new-item-42")
    coordinator = make_coordinator(store, publisher=publisher)
    coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", {})

    result = asyncio.run(coordinator.run_once())

    assert result.status == "succeeded"
    assert result.new_item_id == "new-item-42"
    assert store.get_template(template_id="template-1").current_item_id == "new-item-42"


@pytest.mark.parametrize(
    "template_kwargs",
    [
        {"delivery_content": None},
        {"paused": True},
        {"auto_delivery": False},
        {"auto_republish": False},
    ],
)
def test_missing_link_paused_or_disabled_template_requires_manual_action(store, template_kwargs):
    store.upsert_template(make_template(**template_kwargs))
    coordinator = make_coordinator(store)
    coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", {})

    result = asyncio.run(coordinator.run_once())

    assert result.status == "manual_required"
    assert store.list_jobs()[0].status == "manual_required"


def test_dry_run_never_publishes_or_updates_item_id(store):
    store.upsert_template(make_template())
    publisher = FakePublisher(result="must-not-be-used")
    logs = []
    coordinator = make_coordinator(store, publisher=publisher, logs=logs, dry_run=True)
    coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", {})

    result = asyncio.run(coordinator.run_once())

    assert result.status == "preview"
    assert publisher.calls == []
    assert store.list_jobs()[0].status == "retry"
    assert store.list_jobs()[0].new_item_id is None
    assert store.get_template(template_id="template-1").current_item_id == "item-1"
    assert logs and set(logs[-1]) == {
        "template_id",
        "item_id",
        "job_id",
        "safe_delivery_summary",
    }
    assert SHARED_LINK not in repr(logs)


@pytest.mark.parametrize("publisher_result", [None, "", "   "])
def test_missing_published_id_is_retried_without_item_update(store, publisher_result):
    store.upsert_template(make_template())
    coordinator = make_coordinator(store, publisher=FakePublisher(result=publisher_result))
    coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", {})

    result = asyncio.run(coordinator.run_once())

    assert result.status == "retry"
    assert store.get_template(template_id="template-1").current_item_id == "item-1"


@pytest.mark.parametrize("publisher_result", [123, "item-1"])
def test_invalid_published_id_is_rejected_without_marking_success(store, publisher_result):
    store.upsert_template(make_template())
    coordinator = make_coordinator(store, publisher=FakePublisher(result=publisher_result))
    coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", {})

    result = asyncio.run(coordinator.run_once())

    assert result.status == "retry"
    assert store.list_jobs()[0].new_item_id is None
    assert store.get_template(template_id="template-1").current_item_id == "item-1"


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [("images", ["not-a-url"]), ("category_hint", ""), ("delivery_choice", "")],
)
def test_invalid_material_configuration_goes_directly_to_manual(
    store, field_name, invalid_value
):
    template = make_template()
    setattr(template, field_name, invalid_value)
    store.upsert_template(template)
    publisher = FakePublisher(result="new-item-should-not-be-used")
    coordinator = make_coordinator(store, publisher=publisher)
    coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", {})

    result = asyncio.run(coordinator.run_once())

    assert result.status == "manual_required"
    assert result.reason == "template_config_invalid"
    assert publisher.calls == []


def test_configuration_error_type_is_manual_classification():
    assert issubclass(RepublishConfigurationError, ManualActionRequired)


def test_item_publisher_adapter_awaits_async_extractor_and_keeps_cookie_scope():
    class AsyncPublisher:
        cookie_id = "cookie-1"

        async def publish_item(self, **kwargs):
            return {"data": {"itemId": "new-item-9"}}

        async def extract_published_item_id(self, payload):
            return payload["data"]["itemId"]

    template = make_template()

    result = asyncio.run(ItemPublisherAdapter(AsyncPublisher()).publish(template))

    assert result == "new-item-9"


def test_item_publisher_adapter_rejects_cross_cookie_template_before_publish():
    class Publisher:
        cookie_id = "cookie-other"
        called = False

        async def publish_item(self, **kwargs):
            self.called = True
            return {"data": {"itemId": "new-item-9"}}

    publisher = Publisher()

    with pytest.raises(RepublishConfigurationError, match="cookie"):
        asyncio.run(ItemPublisherAdapter(publisher).publish(make_template()))

    assert publisher.called is False


def test_publish_exception_retries_three_times_then_enters_manual_queue(store):
    store.upsert_template(make_template())
    publisher = FakePublisher(error=RuntimeError("private exception text"))
    current_time = [1000.0]
    coordinator = make_coordinator(store, publisher=publisher, clock=lambda: current_time[0])
    coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", {})

    first = asyncio.run(coordinator.run_once())
    first_job = store.list_jobs()[0]
    current_time[0] = first_job.available_at
    second = asyncio.run(coordinator.run_once())
    second_job = store.list_jobs()[0]
    current_time[0] = second_job.available_at
    third = asyncio.run(coordinator.run_once())
    third_job = store.list_jobs()[0]
    current_time[0] = third_job.available_at
    fourth = asyncio.run(coordinator.run_once())

    assert first.status == "retry"
    assert second.status == "retry"
    assert third.status == "retry"
    assert fourth.status == "manual_required"
    assert [first_job.available_at, second_job.available_at, third_job.available_at] == [
        1300.0,
        2200.0,
        4000.0,
    ]
    assert store.list_jobs()[0].attempts == 3
    assert len(publisher.calls) == 4
    assert "private exception text" not in repr(store.list_jobs())


def test_manual_action_required_enters_manual_queue_immediately(store):
    store.upsert_template(make_template())
    publisher = FakePublisher(error=ManualActionRequired("risk_control"))
    coordinator = make_coordinator(store, publisher=publisher)
    coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", {})

    result = asyncio.run(coordinator.run_once())

    assert result.status == "manual_required"
    assert store.list_jobs()[0].attempts == 0


@pytest.mark.parametrize("bad_context", [{"value": math.nan}, {"value": math.inf}, {"value": -math.inf}])
def test_order_context_rejects_non_finite_json(store, bad_context):
    store.upsert_template(make_template())
    coordinator = make_coordinator(store)

    with pytest.raises(ValueError, match="JSON|finite"):
        coordinator.on_delivery_finalized("order-1", "cookie-1", "item-1", bad_context)


def test_missing_template_is_ignored_without_publishing(store):
    publisher = FakePublisher()
    coordinator = make_coordinator(store, publisher=publisher)

    result = coordinator.on_delivery_finalized("order-1", "cookie-1", "item-404", {})

    assert result.status == "ignored"
    assert result.reason == "template_not_configured"
    assert publisher.calls == []
