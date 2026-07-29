# Task 7 Delivery Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build quantity-aware, idempotent, order-pausing delivery orchestration and connect it to the existing delivery seam without changing the legacy path.

**Architecture:** Add a focused `delivery_orchestration_service.py` with injected inventory, dispatcher, database and optional sender dependencies. Persist one scoped orchestration row per order line, use Task 4 for atomic full-quantity reservations and Task 6 for preparation/commit, and expose explicit prepare/retry/mark methods. Extend Task 6 request context with quantity and idempotency data, and pass the optional context through `XianyuLive._prepare_configured_delivery` while leaving its old defaults unchanged.

**Tech Stack:** Python 3, SQLite, dataclasses, existing Task 4/6 services, pytest, py_compile.

---

### Task 1: Add the orchestration state schema

**Files:**
- Modify: `db_manager.py` near `delivery_finalization_states`
- Test: `tests/test_delivery_orchestration_migration.py`

- [ ] **Step 1: Write the failing migration test**

```python
def test_delivery_orchestration_state_table_has_scoped_unique_key(tmp_path):
    from db_manager import DBManager

    manager = DBManager(str(tmp_path / "delivery.sqlite3"))
    try:
        columns = manager.conn.execute(
            "PRAGMA table_info(delivery_orchestration_states)"
        ).fetchall()
        names = {row[1] for row in columns}
        assert {"user_id", "card_id", "account_id", "order_id", "order_line_id", "quantity", "status", "idempotency_key"} <= names
        indexes = manager.conn.execute(
            "PRAGMA index_list(delivery_orchestration_states)"
        ).fetchall()
        assert any("order_line" in str(row[1]) for row in indexes)
    finally:
        manager.close()
```

- [ ] **Step 2: Run the migration test and verify RED**

Run: `python -m pytest -q tests/test_delivery_orchestration_migration.py::test_delivery_orchestration_state_table_has_scoped_unique_key`

Expected: FAIL because `delivery_orchestration_states` does not exist.

- [ ] **Step 3: Add the table and indexes in `DBManager.init_db()`**

Create `delivery_orchestration_states` with columns `user_id`, `card_id`, `account_id`, `order_id`, `order_line_id`, `quantity`, `mode`, `idempotency_key`, `reservation_id`, `status`, `result_meta`, `last_error_code`, `last_error`, timestamps, and a unique index over `user_id, card_id, account_id, order_id, order_line_id`. Restrict status to `pending`, `paused`, `reserved`, `sending`, `sent`, and `failed`. Use `CREATE TABLE IF NOT EXISTS` and `CREATE UNIQUE INDEX IF NOT EXISTS` so existing databases remain intact.

- [ ] **Step 4: Run the migration test and verify GREEN**

Run: `python -m pytest -q tests/test_delivery_orchestration_migration.py::test_delivery_orchestration_state_table_has_scoped_unique_key`

Expected: PASS.

- [ ] **Step 5: Commit the schema change**

```powershell
git add db_manager.py tests/test_delivery_orchestration_migration.py
git commit -m "feat: add delivery orchestration state"
```

### Task 2: Add quantity normalization, idempotency and card orchestration tests

**Files:**
- Create: `tests/test_delivery_quantity_contract.py`
- Create: `tests/test_delivery_idempotency.py`
- Create: `delivery_orchestration_service.py`

- [ ] **Step 1: Write failing service contract tests**

Cover these exact behaviors with `DBManager(tmp_path)`, `CardInventoryService`, `DeliveryConfigService`, `DeliveryDispatcher`, and a fake sender:

```python
def test_missing_quantity_defaults_to_one_and_order_line_falls_back_to_item():
    request = DeliveryOrchestrationRequest(
        user_id=1, card_id=7, account_id="account-a", order_id="order-1",
        order_line_id=None, item_id="item-7", quantity=None,
        delivery_config={"mode": "fixed_link", "url": "https://example.test/a"},
    )
    result = service.prepare(request)
    assert result["quantity"] == 1
    assert result["order_line_id"] == "item-7"
    assert result["status"] == "sending"


@pytest.mark.parametrize("raw", [0, -1, "abc", "1.5", True, 101])
def test_invalid_quantity_is_rejected_without_state(raw):
    request = make_request(quantity=raw)
    with pytest.raises(DeliveryOrchestrationError) as error:
        service.prepare(request)
    assert error.value.code == "invalid_quantity"


def test_insufficient_card_inventory_pauses_whole_order_without_partial_reservation():
    inventory.import_items(7, 1, "account-a", ["a", "b"])
    result = service.prepare(make_card_request(quantity=3))
    assert result["status"] == "paused"
    assert result["error_code"] == "insufficient_inventory"
    assert inventory.get_inventory_summary(7, 1, "account-a")["available"] == 2
    assert sender.calls == []


def test_quantity_three_commits_three_distinct_cards_and_sends_once():
    inventory.import_items(7, 1, "account-a", ["a", "b", "c"])
    result = service.orchestrate(make_card_request(quantity="3"), sender)
    assert result["status"] == "sent"
    assert len(sender.calls) == 1
    assert len(sender.calls[0]["contents"]) == 3
    assert len(set(sender.calls[0]["contents"])) == 3
    assert inventory.get_inventory_summary(7, 1, "account-a")["sent"] == 3
```

Also add fixed-link and provider assertions, duplicate callback assertions, explicit retry after send failure, and scope/order-line separation assertions.

- [ ] **Step 2: Run the new service tests and verify RED**

Run: `python -m pytest -q tests/test_delivery_quantity_contract.py tests/test_delivery_idempotency.py`

Expected: FAIL because `delivery_orchestration_service.py` and its state operations do not yet exist.

- [ ] **Step 3: Implement the minimal service API**

Define `DeliveryOrchestrationRequest`, `DeliveryOrchestrationError`, `MAX_DELIVERY_QUANTITY = 100`, `normalize_quantity()`, and `normalize_order_line_id()`. Implement `DeliveryOrchestrationService.prepare()`, `orchestrate()`, `retry()`, `mark_sent()`, and `mark_failed()` using the injected DB connection and the new state table. `prepare()` must create/read one state row, reserve the complete quantity for `imported_card`/`generated_card`, call `DeliveryDispatcher.prepare()` once, and return a result with `contents`, `quantity`, `status`, `idempotency_key`, and non-sensitive error metadata. If dispatcher preparation fails after a reservation is created, release that reservation. If sending fails after commit, retain the reservation and set `failed`; retry must call dispatcher again with the same reservation and never call reserve again.

- [ ] **Step 4: Run the service tests and verify GREEN**

Run: `python -m pytest -q tests/test_delivery_quantity_contract.py tests/test_delivery_idempotency.py`

Expected: PASS with no card secret in captured logs.

- [ ] **Step 5: Commit the orchestration service**

```powershell
git add delivery_orchestration_service.py tests/test_delivery_quantity_contract.py tests/test_delivery_idempotency.py
git commit -m "feat: orchestrate quantity-aware idempotent delivery"
```

### Task 3: Pass quantity and idempotency through Task 6

**Files:**
- Modify: `delivery_adapter_service.py`
- Test: `tests/test_delivery_adapter_service.py`

- [ ] **Step 1: Add failing adapter assertions**

Extend the provider test to build a `DeliveryRequest(quantity=3, idempotency_key="scope-key")` and assert the provider payload includes `quantity: 3` and `idempotency_key: "scope-key"`. Add a card test asserting a committed three-card reservation returns three contents to the orchestration service without a second commit.

- [ ] **Step 2: Run the focused adapter tests and verify RED**

Run: `python -m pytest -q tests/test_delivery_adapter_service.py -k "provider or card_modes"`

Expected: FAIL because the request has no quantity/idempotency fields or provider context.

- [ ] **Step 3: Extend `DeliveryRequest` and provider context**

Add optional `quantity: int = 1` and `idempotency_key: str | None = None` fields. Include them in the provider adapter context only as non-secret order metadata. Keep the existing exact response shapes unchanged so old tests and callers remain compatible.

- [ ] **Step 4: Run adapter and related tests**

Run: `python -m pytest -q tests/test_delivery_adapter_service.py tests/test_delivery_quantity_contract.py tests/test_delivery_idempotency.py`

Expected: PASS.

- [ ] **Step 5: Commit the adapter seam**

```powershell
git add delivery_adapter_service.py tests/test_delivery_adapter_service.py
git commit -m "feat: pass delivery quantity and idempotency context"
```

### Task 4: Integrate the orchestration context with `_auto_delivery`

**Files:**
- Modify: `XianyuAutoAsync.py`
- Test: `tests/test_delivery_adapter_service.py`
- Test: `tests/test_delivery_quantity_contract.py`

- [ ] **Step 1: Add failing seam tests**

Call `_prepare_configured_delivery()` with `quantity=3`, `order_line_id="line-1"`, and `idempotency_key="scope-key"`; assert the captured `DeliveryRequest` receives all three values. Add a compatibility test with omitted optional arguments asserting the old reservation-only call still works.

- [ ] **Step 2: Run the seam tests and verify RED**

Run: `python -m pytest -q tests/test_delivery_adapter_service.py -k "seam or auto_delivery_routes"`

Expected: FAIL because `_prepare_configured_delivery()` does not accept or forward the new fields.

- [ ] **Step 3: Add optional forwarding and a focused orchestration helper**

Extend `_prepare_configured_delivery()` with `quantity=1`, `order_line_id=None`, and `idempotency_key=None`, add them to `DeliveryRequest.context`, and pass them as request fields. Add `_get_delivery_orchestration_service()` with injected DB/inventory/config/dispatcher construction and `_orchestrate_configured_delivery()` as the explicit entry point for new configured-order callers. Do not alter legacy `_auto_delivery` behavior when `delivery_reservation_id` is absent.

- [ ] **Step 4: Run all delivery seam tests**

Run: `python -m pytest -q tests/test_delivery_adapter_service.py tests/test_delivery_quantity_contract.py tests/test_delivery_idempotency.py`

Expected: PASS.

- [ ] **Step 5: Commit the seam integration**

```powershell
git add XianyuAutoAsync.py tests/test_delivery_adapter_service.py tests/test_delivery_quantity_contract.py
git commit -m "feat: connect orchestration context to auto delivery seam"
```

### Task 5: Verify the whole repository and commit the final Task 7 change

**Files:**
- Modify only files required by the preceding tasks.

- [ ] **Step 1: Run Task 7 and related tests**

Run: `python -m pytest -q tests/test_delivery_orchestration_migration.py tests/test_delivery_quantity_contract.py tests/test_delivery_idempotency.py tests/test_delivery_adapter_service.py tests/test_card_inventory_service.py`

Expected: PASS.

- [ ] **Step 2: Run syntax and full test verification**

Run: `python -m compileall -q db_manager.py card_inventory_service.py delivery_config_service.py delivery_adapter_service.py delivery_orchestration_service.py XianyuAutoAsync.py reply_server.py; python -m pytest -q`

Expected: exit code 0 and no test failures. Run `node --check static/js/app.js` only if the final diff contains JavaScript changes.

- [ ] **Step 3: Inspect the diff and sensitive-data contract**

Run: `git diff --check; git status --short; git diff --stat; git diff -- delivery_orchestration_service.py delivery_adapter_service.py XianyuAutoAsync.py db_manager.py`

Confirm no card plaintext, provider token, or provider response body is logged or written to orchestration metadata, and no Task 8/Task 9 files changed.

- [ ] **Step 4: Commit the verified Task 7 implementation**

```powershell
git add db_manager.py delivery_adapter_service.py delivery_orchestration_service.py XianyuAutoAsync.py tests
git commit -m "feat: add quantity-aware idempotent delivery orchestration"
```
