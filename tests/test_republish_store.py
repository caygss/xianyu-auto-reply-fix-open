import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from republish_models import RepublishTemplate
from republish_store import RepublishStore, RepublishStoreError, RepublishTemplateConflictError


def make_template(
    template_id="template-1",
    cookie_id="cookie-1",
    current_item_id="item-1",
    *,
    delivery_content="delivery-content",
    paused=False,
):
    return RepublishTemplate(
        template_id=template_id,
        cookie_id=cookie_id,
        current_item_id=current_item_id,
        title="Reusable item",
        description="A reusable description",
        images=["https://img.example/item.jpg"],
        current_price=12.5,
        original_price=20.0,
        delivery_choice="digital",
        post_price=0.0,
        can_self_pickup=False,
        category_hint="book",
        delivery_content=delivery_content,
        sku_delivery={"default": "delivery-content"},
        auto_delivery=True,
        auto_republish=True,
        paused=paused,
    )


@pytest.fixture
def store(tmp_path):
    return RepublishStore(tmp_path / "republish.sqlite3")


def test_template_insert_and_upsert_preserves_template_id(store):
    created = store.upsert_template(make_template())
    updated = store.upsert_template(
        make_template(
            template_id="different-template-id",
            delivery_content="updated-delivery",
        )
    )

    assert created.template_id == "template-1"
    assert updated.template_id == created.template_id
    assert store.get_template(template_id="template-1").delivery_content == "updated-delivery"
    assert store.get_template(template_id="different-template-id") is None


def test_atomic_create_rejects_duplicate_item_without_overwriting_existing_template(store):
    created = store.create_template(make_template(delivery_content="first"))

    with pytest.raises(RepublishTemplateConflictError):
        store.create_template(make_template(template_id="second", delivery_content="second"))

    persisted = store.get_template(template_id=created.template_id)
    assert persisted.delivery_content == "first"
    assert store.get_template(template_id="second") is None


def test_list_recent_jobs_for_cookie_is_bounded_and_single_query_shape(store):
    store.create_template(make_template())
    for index in range(5):
        store.enqueue_after_delivery("template-1", "item-1", f"order-{index}")

    jobs = store.list_recent_jobs("cookie-1", limit=2)

    assert len(jobs) == 2
    assert jobs[0].created_at >= jobs[1].created_at


def test_list_latest_jobs_by_template_ids_returns_one_latest_job_per_template(store):
    store.create_template(make_template(template_id="template-1"))
    first = store.enqueue_after_delivery("template-1", "item-1", "order-1")
    latest = store.enqueue_after_delivery("template-1", "item-1", "order-2")
    store.create_template(make_template(template_id="template-3", current_item_id="item-3"))
    third = store.enqueue_after_delivery("template-3", "item-3", "order-3")

    result = store.list_latest_jobs_by_template_ids(
        ["template-1", "template-2", "template-3"]
    )

    jobs = store.list_jobs(template_id="template-1")
    expected_latest = max(jobs, key=lambda job: (job.updated_at, job.created_at, job.job_id))
    assert set(result) == {"template-1", "template-3"}
    assert result["template-1"].job_id == expected_latest.job_id
    assert result["template-1"].job_id in {first.job_id, latest.job_id}
    assert result["template-3"].job_id == third.job_id


def test_template_can_be_found_by_cookie_and_current_item(store):
    store.upsert_template(make_template())

    found = store.get_template(cookie_id="cookie-1", current_item_id="item-1")

    assert found is not None
    assert found.template_id == "template-1"


def test_templates_with_same_delivery_content_are_isolated(store):
    store.upsert_template(make_template("template-1", "cookie-1", "item-1"))
    store.upsert_template(make_template("template-2", "cookie-2", "item-2"))

    templates = store.list_templates()

    assert {template.template_id for template in templates} == {"template-1", "template-2"}
    assert all(template.delivery_content == "delivery-content" for template in templates)


def test_special_product_has_independent_current_item_link(store):
    store.upsert_template(make_template("normal", "cookie-1", "item-normal"))
    store.upsert_template(make_template("special", "cookie-1", "item-special"))

    assert store.get_template(cookie_id="cookie-1", current_item_id="item-special").template_id == "special"
    assert store.get_template(cookie_id="cookie-1", current_item_id="item-normal").template_id == "normal"


def test_paused_template_is_listed_but_not_enqueued(store):
    store.upsert_template(make_template(paused=True))

    assert store.list_templates()[0].paused is True
    assert store.enqueue_after_delivery("template-1", "item-1", "order-1") is None


def test_enqueue_rejects_source_item_that_is_not_template_current_item(store):
    store.upsert_template(make_template())

    with pytest.raises(RepublishStoreError, match="source_item_id"):
        store.enqueue_after_delivery("template-1", "source-1", "order-1")

    assert store.list_jobs() == []


def test_duplicate_trigger_order_is_idempotent(store):
    store.upsert_template(make_template())

    first = store.enqueue_after_delivery("template-1", "item-1", "order-1")
    second = store.enqueue_after_delivery("template-1", "item-1", "order-1")

    assert first.job_id == second.job_id
    assert len(store.list_jobs()) == 1


def test_job_pending_running_succeeded_flow_updates_template(store):
    store.upsert_template(make_template())
    job = store.enqueue_after_delivery("template-1", "item-1", "order-1", available_at=10)

    claimed = store.claim_due_job(now=10)
    succeeded = store.mark_succeeded(claimed.job_id, "new-item-1", now=11)

    assert job.status == "pending"
    assert claimed.status == "running"
    assert succeeded.status == "succeeded"
    assert succeeded.old_item_id == "item-1"
    assert succeeded.new_item_id == "new-item-1"
    assert store.get_template(template_id="template-1").current_item_id == "new-item-1"


def test_mark_succeeded_rejects_concurrent_template_rotation_without_overwrite(store):
    store.upsert_template(make_template())
    job = store.enqueue_after_delivery("template-1", "item-1", "order-1", available_at=10)
    claimed = store.claim_due_job(now=10)
    with store._connection() as connection:
        connection.execute(
            "UPDATE republish_templates SET current_item_id = ? WHERE template_id = ?",
            ("item-2", "template-1"),
        )

    with pytest.raises(RepublishStoreError, match="current_item_id"):
        store.mark_succeeded(claimed.job_id, "new-item-1", now=11)

    assert store.get_template(template_id="template-1").current_item_id == "item-2"
    assert store.list_jobs()[0].status == "running"


@pytest.mark.parametrize("new_item_id", ["item-1"])
def test_mark_succeeded_rejects_new_id_equal_to_source_or_old_item(store, new_item_id):
    store.upsert_template(make_template())
    store.enqueue_after_delivery("template-1", "item-1", "order-1", available_at=10)
    claimed = store.claim_due_job(now=10)

    with pytest.raises(ValueError, match="new_item_id|source_item_id|old_item_id"):
        store.mark_succeeded(claimed.job_id, new_item_id, now=11)

    assert store.get_template(template_id="template-1").current_item_id == "item-1"
    job = store.list_jobs()[0]
    assert job.status == "running"
    assert job.new_item_id is None


def test_manual_required_flow_persists_error(store):
    store.upsert_template(make_template())
    store.enqueue_after_delivery("template-1", "item-1", "order-1")
    claimed = store.claim_due_job(now=10**12)

    manual = store.mark_manual_required(claimed.job_id, "listing requires review")

    with sqlite3.connect(store.db_path) as connection:
        template_status, template_error = connection.execute(
            "SELECT last_status, last_error FROM republish_templates WHERE template_id = ?",
            ("template-1",),
        ).fetchone()

    assert manual.status == "manual_required"
    assert manual.last_error == "listing requires review"
    assert template_status == "manual_required"
    assert template_error == "listing requires review"


def test_retry_uses_supplied_time_error_and_attempts(store):
    store.upsert_template(make_template())
    store.enqueue_after_delivery("template-1", "item-1", "order-1")
    claimed = store.claim_due_job(now=10**12)

    retried = store.mark_retry(
        claimed.job_id,
        available_at=1234,
        error="temporary failure",
        attempts=3,
    )

    assert retried.status == "retry"
    assert retried.available_at == 1234
    assert retried.last_error == "temporary failure"
    assert retried.attempts == 3


def test_job_transitions_update_template_latest_status_fields(store):
    store.upsert_template(make_template())
    store.enqueue_after_delivery("template-1", "item-1", "order-1")
    claimed = store.claim_due_job(now=10**12)
    store.mark_retry(claimed.job_id, available_at=10**12 + 1, error="temporary failure", attempts=1)

    with sqlite3.connect(store.db_path) as connection:
        status, last_error = connection.execute(
            "SELECT last_status, last_error FROM republish_templates WHERE template_id = ?",
            ("template-1",),
        ).fetchone()

    assert status == "retry"
    assert last_error == "temporary failure"


def test_due_claim_and_duplicate_claim_protection(store):
    store.upsert_template(make_template())
    store.enqueue_after_delivery("template-1", "item-1", "order-1", available_at=5)
    barrier = threading.Barrier(2)

    def claim():
        with RepublishStore(store.db_path) as worker:
            barrier.wait()
            job = worker.claim_due_job(now=5)
            return None if job is None else job.job_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed_ids = list(executor.map(lambda _: claim(), range(2)))

    claimed = [value for value in claimed_ids if value is not None]
    assert len(claimed) == 1
    assert claimed_ids.count(None) == 1


def test_due_job_can_be_claimed_by_new_store_instance_after_restart(tmp_path):
    db_path = tmp_path / "restart.sqlite3"
    first_store = RepublishStore(db_path)
    first_store.upsert_template(make_template())
    first_store.enqueue_after_delivery("template-1", "item-1", "order-1", available_at=1)
    first_store.close()

    with RepublishStore(db_path) as restarted_store:
        claimed = restarted_store.claim_due_job(now=1)

    assert claimed is not None
    assert claimed.status == "running"


def test_job_order_context_is_strictly_persisted_and_survives_restart(tmp_path):
    db_path = tmp_path / "context.sqlite3"
    with RepublishStore(db_path) as store:
        store.upsert_template(make_template())
        store.enqueue_after_delivery(
            "template-1",
            "item-1",
            "order-1",
            order_context={"sku_id": "sku-1", "quantity": 1},
        )

    with RepublishStore(db_path) as restarted:
        job = restarted.list_jobs()[0]

    assert job.order_context == {"sku_id": "sku-1", "quantity": 1}


@pytest.mark.parametrize("bad_context", [{"value": math.nan}, {"value": math.inf}, {"value": -math.inf}])
def test_job_order_context_rejects_non_finite_json(store, bad_context):
    store.upsert_template(make_template())

    with pytest.raises(ValueError, match="JSON|finite"):
        store.enqueue_after_delivery("template-1", "item-1", "order-1", order_context=bad_context)


def test_old_jobs_table_is_migrated_idempotently_with_order_context_column(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE republish_templates (
                template_id TEXT PRIMARY KEY, cookie_id TEXT NOT NULL,
                current_item_id TEXT NOT NULL, template_json TEXT NOT NULL,
                delivery_content TEXT, sku_delivery_json TEXT,
                auto_delivery INTEGER NOT NULL DEFAULT 0,
                auto_republish INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0,
                last_status TEXT NOT NULL DEFAULT 'ready', last_error TEXT,
                last_success_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(cookie_id, current_item_id)
            );
            CREATE TABLE republish_jobs (
                job_id TEXT PRIMARY KEY, template_id TEXT NOT NULL,
                source_item_id TEXT NOT NULL, trigger_order_id TEXT NOT NULL,
                status TEXT NOT NULL, available_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                old_item_id TEXT NOT NULL, new_item_id TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(template_id, trigger_order_id)
            );
            """
        )

    RepublishStore(db_path).ensure_schema()
    RepublishStore(db_path).ensure_schema()

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(republish_jobs)").fetchall()
        }
    assert "order_context_json" in columns


def test_store_uses_only_injected_temporary_sqlite_path(tmp_path):
    db_path = tmp_path / "isolated.sqlite3"
    store = RepublishStore(db_path)
    store.upsert_template(make_template())

    assert db_path.exists()
    assert not (tmp_path / "data" / "xianyu_data.db").exists()


def test_store_connections_enable_foreign_keys_busy_timeout_and_wal(store):
    with store._connection() as connection:
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert foreign_keys == 1
    assert busy_timeout == 5000
    assert journal_mode.lower() == "wal"


def test_republish_repr_does_not_expose_delivery_or_error_content(store):
    template = make_template(delivery_content="delivery-secret")
    template.title = "title-secret"
    template.description = "description-secret"
    template.images = ["https://image.example/secret-image"]
    template.sku_delivery = {"secret": "sku-secret"}
    store.upsert_template(template)
    job = store.enqueue_after_delivery(template.template_id, "item-1", "order-1")
    job.order_context = {"secret": "context-secret"}
    job.last_error = "error-secret"

    for secret in (
        "delivery-secret",
        "sku-secret",
        "title-secret",
        "description-secret",
        "secret-image",
    ):
        assert secret not in repr(template)
    for secret in ("context-secret", "error-secret"):
        assert secret not in repr(job)


def test_republish_test_file_has_a_precise_gitignore_exception():
    lines = (Path(__file__).parents[1] / ".gitignore").read_text(encoding="utf-8").splitlines()
    exception_index = lines.index("!tests/test_republish_store.py")
    broad_test_rules = [index for index, line in enumerate(lines) if line == "test_*.py"]

    assert broad_test_rules
    assert exception_index > max(broad_test_rules)


def test_model_rejects_missing_core_ids_and_tolerates_missing_optional_fields():
    with pytest.raises(ValueError):
        RepublishTemplate(template_id="", cookie_id="cookie", current_item_id="item")

    template = RepublishTemplate.from_dict(
        {"template_id": "template-1", "cookie_id": "cookie-1", "current_item_id": "item-1"}
    )

    assert template.title == ""
    assert template.images == []
    assert template.paused is False


@pytest.mark.parametrize("field_name", ["current_price", "original_price", "post_price"])
@pytest.mark.parametrize("invalid_value", [math.nan, math.inf, -math.inf])
def test_template_rejects_non_finite_numeric_values(field_name, invalid_value):
    values = make_template().to_dict()
    values[field_name] = invalid_value

    with pytest.raises(ValueError, match="finite"):
        RepublishTemplate.from_dict(values)


@pytest.mark.parametrize("invalid_value", [math.nan, math.inf, -math.inf])
def test_enqueue_rejects_non_finite_available_at(store, invalid_value):
    store.upsert_template(make_template())

    with pytest.raises(ValueError, match="finite"):
        store.enqueue_after_delivery(
            "template-1", "item-1", "order-1", available_at=invalid_value
        )


def test_template_json_serialization_rejects_non_finite_values(store):
    template = make_template()
    template.sku_delivery = {"value": math.nan}

    with pytest.raises(RepublishStoreError, match="JSON"):
        store.upsert_template(template)


def test_from_dict_rejects_non_finite_numeric_values():
    for invalid_value in (math.nan, math.inf, -math.inf):
        values = make_template().to_dict()
        values["current_price"] = invalid_value

        with pytest.raises(ValueError, match="finite"):
            RepublishTemplate.from_dict(values)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_from_json_rejects_non_finite_json_constants(constant):
    raw = (
        '{"template_id":"template-1","cookie_id":"cookie-1",'
        f'"current_item_id":"item-1","current_price":{constant}}}'
    )

    with pytest.raises(ValueError, match="JSON constant"):
        RepublishTemplate.from_json(raw)


def test_from_json_rejects_non_finite_nested_json_constants():
    raw = (
        '{"template_id":"template-1","cookie_id":"cookie-1",'
        '"current_item_id":"item-1","sku_delivery":{"value":NaN}}'
    )

    with pytest.raises(ValueError, match="JSON constant"):
        RepublishTemplate.from_json(raw)
