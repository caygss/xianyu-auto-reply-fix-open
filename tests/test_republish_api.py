from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import HTTPException

import reply_server
from republish_store import RepublishStore
from republish_store import RepublishTemplateConflictError


USER = {"user_id": 1, "username": "owner"}
OTHER_USER = {"user_id": 2, "username": "other"}


def _item(item_id: str = "item-1", *, with_images: bool = True) -> dict:
    detail = {
        "images": [{"url": "https://img.example/item.jpg"}] if with_images else [],
        "delivery_choice": "包邮",
        "category": "数字商品",
    }
    return {
        "item_id": item_id,
        "item_title": "测试网盘商品",
        "item_description": "测试商品描述",
        "item_price": "9.90",
        "item_detail": __import__("json").dumps(detail, ensure_ascii=False),
        "item_detail_parsed": detail,
        "item_category": "数字商品",
    }


@pytest.fixture()
def api_state(tmp_path, monkeypatch):
    store = RepublishStore(tmp_path / "republish.sqlite3")
    monkeypatch.setattr(reply_server, "_REPUBLISH_STORE", store)
    items = {("cookie-a", "item-1"): _item(), ("cookie-b", "item-2"): _item("item-2")}

    def cookies(user_id=None):
        if user_id == 1:
            return {"cookie-a": "masked-value", "cookie-b": "masked-value"}
        if user_id == 2:
            return {"cookie-other": "masked-value"}
        return {}

    monkeypatch.setattr(reply_server.db_manager, "get_all_cookies", cookies)
    monkeypatch.setattr(reply_server.db_manager, "get_item_info", lambda cid, iid: items.get((cid, iid)))
    return store, items


def _create(cookie_id="cookie-a", item_id="item-1", **overrides):
    payload = {
        "cookie_id": cookie_id,
        "item_id": item_id,
        "delivery_content": "https://pan.example/s/abc?pwd=xyz",
        "sku_delivery": {"sku-red": "https://pan.example/s/red"},
        "auto_delivery": True,
        "auto_republish": True,
    }
    payload.update(overrides)
    return reply_server.create_republish_template(
        reply_server.RepublishTemplateCreateRequest(**payload), USER
    )


def test_create_reads_synced_item_and_returns_only_safe_summary(api_state):
    store, _ = api_state

    response = _create()

    assert response["template"]["current_item_id"] == "item-1"
    assert response["template"]["delivery_summary"].startswith("delivery:")
    assert "pan.example" not in str(response)
    assert "delivery_content" not in response["template"]
    assert store.list_templates("cookie-a")


def test_template_detail_returns_sensitive_delivery_fields_only_after_ownership_check(api_state):
    created = _create()
    template_id = created["template"]["template_id"]

    detail = reply_server.get_republish_template_detail(template_id, USER)

    assert detail["template"]["delivery_content"].endswith("pwd=xyz")
    assert detail["template"]["sku_delivery"] == {"sku-red": "https://pan.example/s/red"}
    assert isinstance(detail["dry_run"], bool)
    assert "order_context" not in detail["template"]

    with pytest.raises(HTTPException) as denied:
        reply_server.get_republish_template_detail(template_id, OTHER_USER)
    assert denied.value.status_code == 403


def test_list_reports_runtime_dry_run_and_patch_without_detail_keeps_links(api_state, monkeypatch):
    created = _create()
    template_id = created["template"]["template_id"]
    monkeypatch.setattr(reply_server, "_REPUBLISH_RUNTIME_INFO", {"dry_run": False})

    listed = reply_server.list_republish_templates("cookie-a", USER)
    assert listed["dry_run"] is False
    assert "delivery_content" not in listed["templates"][0]

    updated = reply_server.update_republish_template(
        template_id,
        reply_server.RepublishTemplateUpdateRequest(auto_delivery=False),
        USER,
    )
    assert updated["template"]["auto_delivery"] is False
    persisted = api_state[0].get_template(template_id=template_id)
    assert persisted.delivery_content.endswith("pwd=xyz")
    assert persisted.sku_delivery == {"sku-red": "https://pan.example/s/red"}


def test_list_templates_uses_one_batch_latest_job_query(api_state, monkeypatch):
    store, items = api_state
    items[("cookie-a", "item-2")] = _item("item-2")
    first = _create(item_id="item-1")
    second = _create(item_id="item-2")
    template_ids = [first["template"]["template_id"], second["template"]["template_id"]]
    calls = []

    def batch_query(ids):
        calls.append(list(ids))
        return {}

    monkeypatch.setattr(store, "list_latest_jobs_by_template_ids", batch_query)

    def unexpected_n_plus_one(*args, **kwargs):
        raise AssertionError("template listing must not query jobs one template at a time")

    monkeypatch.setattr(store, "list_jobs", unexpected_n_plus_one)

    response = reply_server.list_republish_templates("cookie-a", USER)

    assert len(calls) == 1
    assert set(calls[0]) == set(template_ids)
    assert [item["last_result"]["status"] for item in response["templates"]] == [
        "ready",
        "ready",
    ]


def test_create_uses_atomic_store_conflict_as_http_409(api_state, monkeypatch):
    original = api_state[0].create_template

    def conflict(template):
        raise RepublishTemplateConflictError("duplicate template")

    monkeypatch.setattr(api_state[0], "create_template", conflict)
    with pytest.raises(HTTPException) as duplicate:
        _create()
    assert duplicate.value.status_code == 409
    monkeypatch.setattr(api_state[0], "create_template", original)


def test_create_rejects_unknown_item_empty_link_and_missing_images(api_state):
    with pytest.raises(HTTPException) as unknown:
        _create(item_id="missing")
    assert unknown.value.status_code == 404

    with pytest.raises(HTTPException) as empty:
        _create(delivery_content=" ")
    assert empty.value.status_code == 400

    _, items = api_state
    items[("cookie-a", "item-no-image")] = _item("item-no-image", with_images=False)
    with pytest.raises(HTTPException) as no_images:
        _create(item_id="item-no-image")
    assert no_images.value.status_code == 400


def test_create_rejects_duplicate_and_second_owned_account(api_state):
    _create()
    with pytest.raises(HTTPException) as duplicate:
        _create()
    assert duplicate.value.status_code == 409

    with pytest.raises(HTTPException) as second_account:
        _create(cookie_id="cookie-b", item_id="item-2")
    assert second_account.value.status_code == 400


def test_cross_user_template_access_is_rejected(api_state):
    store, _ = api_state
    template = _create()

    with pytest.raises(HTTPException) as read_error:
        reply_server.list_republish_templates("cookie-a", OTHER_USER)
    assert read_error.value.status_code == 403

    with pytest.raises(HTTPException) as patch_error:
        reply_server.update_republish_template(
            template["template"]["template_id"],
            reply_server.RepublishTemplateUpdateRequest(delivery_content="https://other.example"),
            OTHER_USER,
        )
    assert patch_error.value.status_code == 403
    assert store.get_template(template_id=template["template"]["template_id"]).delivery_content.endswith("xyz")


def test_patch_updates_link_sku_and_flags_without_changing_template_id(api_state):
    created = _create()
    template_id = created["template"]["template_id"]

    response = reply_server.update_republish_template(
        template_id,
        reply_server.RepublishTemplateUpdateRequest(
            delivery_content="https://pan.example/s/new",
            sku_delivery={"sku-blue": "https://pan.example/s/blue"},
            auto_delivery=False,
            auto_republish=False,
        ),
        USER,
    )

    assert response["template"]["template_id"] == template_id
    assert response["template"]["auto_delivery"] is False
    assert response["template"]["auto_republish"] is False
    assert "pan.example" not in str(response)


def test_pause_resume_and_check_now_only_enqueue_a_pending_job(api_state):
    created = _create()
    template_id = created["template"]["template_id"]

    paused = reply_server.pause_republish_template(
        template_id, reply_server.RepublishPauseRequest(paused=True), USER
    )
    assert paused["template"]["paused"] is True
    with pytest.raises(HTTPException) as paused_check:
        reply_server.check_republish_template_now(template_id, USER)
    assert paused_check.value.status_code == 409

    resumed = reply_server.pause_republish_template(
        template_id, reply_server.RepublishPauseRequest(paused=False), USER
    )
    assert resumed["template"]["paused"] is False
    checked = reply_server.check_republish_template_now(template_id, USER)
    assert checked["job"]["status"] == "pending"
    assert checked["job"]["new_item_id"] is None


def test_jobs_are_scoped_and_redact_full_errors(api_state):
    store, _ = api_state
    created = _create()
    template_id = created["template"]["template_id"]
    job = store.enqueue_after_delivery(template_id, "item-1", "order-1")
    claimed = store.claim_due_job()
    store.mark_manual_required(claimed.job_id, "https://pan.example/s/secret?token=full")

    response = reply_server.list_republish_jobs("cookie-a", USER)
    assert response["jobs"][0]["job_id"] == job.job_id
    assert response["jobs"][0]["old_item_id"] == "item-1"
    assert "pan.example" not in str(response)
    assert "order_context" not in response["jobs"][0]

    with pytest.raises(HTTPException) as other:
        reply_server.list_republish_jobs("cookie-a", OTHER_USER)
    assert other.value.status_code == 403


def test_jobs_endpoint_has_bounded_recent_results(api_state):
    store, _ = api_state
    created = _create()
    for index in range(8):
        store.enqueue_after_delivery(
            created["template"]["template_id"],
            "item-1",
            f"order-{index}",
        )

    response = reply_server.list_republish_jobs("cookie-a", USER, limit=3)
    assert len(response["jobs"]) == 3


def test_republish_routes_are_authenticated_and_present():
    routes = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in reply_server.app.routes
        if getattr(route, "path", "").startswith("/api/republish/")
    }
    assert ("/api/republish/templates", ("GET",)) in routes
    assert ("/api/republish/templates", ("POST",)) in routes
    assert ("/api/republish/templates/{template_id}", ("PATCH",)) in routes
    assert ("/api/republish/templates/{template_id}/pause", ("POST",)) in routes
    assert ("/api/republish/templates/{template_id}/check-now", ("POST",)) in routes
    assert ("/api/republish/jobs", ("GET",)) in routes
