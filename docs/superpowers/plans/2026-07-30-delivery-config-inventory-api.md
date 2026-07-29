# Task 5 Delivery Config and Card Inventory API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add authenticated, account/product-scoped delivery configuration CRUD and card inventory APIs while reusing Task 4 inventory logic and preserving `/setup/status` delivery compatibility.

**Architecture:** Add a focused `DeliveryConfigService` backed by a new encrypted SQLite table. Add thin FastAPI handlers in `reply_server.py` that validate the current user owns both the cookie account and card, then translate service errors into safe Chinese responses. Use one global `CardInventoryService(db_manager)` for all inventory handlers; do not duplicate inventory behavior.

**Tech Stack:** Python 3, FastAPI, Pydantic 2, SQLite, existing DBManager Fernet encryption, `CardInventoryService`, pytest.

---

### Task 1: Define the failing API contract tests

**Files:**
- Create: `tests/test_delivery_config_inventory_api.py`
- Read: `reply_server.py`, `card_inventory_service.py`, `tests/test_republish_api.py`

- [ ] **Step 1: Write route, authentication, scope, validation, CRUD, inventory, and setup compatibility tests**

  Use direct route calls for deterministic service contracts and inspect `reply_server.app.routes` for FastAPI auth dependencies. Cover:

  - all new route/method pairs;
  - missing auth dependency and cross-user account/card rejection;
  - all four delivery modes, fixed-link URL validation, empty/unknown mode errors;
  - PUT then GET/DELETE response containing `config_summary` but never the secret URL/provider key;
  - inventory settings, import, generate, summary, masked preview, and no cleartext secret in captured logs;
  - `/setup/status` summary becomes configured after a saved config and preserves template fields.

- [ ] **Step 2: Run the new tests and verify expected RED**

  Run:

  ```powershell
  .\venv\Scripts\python.exe -m pytest -q tests/test_delivery_config_inventory_api.py
  ```

  Expected: FAIL because the Task 5 service, routes, storage, and response models do not exist.

- [ ] **Step 3: Commit the failing contract tests**

  ```powershell
  git add tests/test_delivery_config_inventory_api.py
  git commit -m "test: define delivery config and inventory API contract"
  ```

### Task 2: Persist and validate delivery configurations

**Files:**
- Create: `delivery_config_service.py`
- Modify: `db_manager.py` in `init_db()` schema creation area
- Test: `tests/test_delivery_config_inventory_api.py`

- [ ] **Step 1: Add the minimal encrypted configuration service**

  Implement `DeliveryConfigError`, mode validation, `http`/`https` URL validation, encrypted JSON storage, deterministic safe summaries, scoped get/upsert/delete, and account configured counting. Do not log raw config.

- [ ] **Step 2: Run delivery service tests and confirm the service contract is green**

  ```powershell
  .\venv\Scripts\python.exe -m pytest -q tests/test_delivery_config_inventory_api.py -k delivery
  ```

- [ ] **Step 3: Keep summary output secret-free during refactor**

  Assert the full URL, provider key, token, password, and card secret are absent from returned dictionaries and captured logs.

### Task 3: Add authenticated delivery-config API handlers

**Files:**
- Modify: `reply_server.py`
- Test: `tests/test_delivery_config_inventory_api.py`

- [ ] **Step 1: Add request models and common scope/error helpers**

  Require `account_id` query parameters, call `_ensure_cookie_access`, call `db_manager.get_card_by_id(card_id, current_user['user_id'])`, and translate `DeliveryConfigError` without leaking its config payload.

- [ ] **Step 2: Add GET/PUT/DELETE handlers**

  Return `mode`, `card_id`, `account_id`, and `config_summary`; PUT accepts only `mode` and `config`; DELETE is idempotent only for an existing scoped config and returns a safe deletion result.

- [ ] **Step 3: Run delivery API tests and confirm green**

  ```powershell
  .\venv\Scripts\python.exe -m pytest -q tests/test_delivery_config_inventory_api.py -k delivery
  ```

### Task 4: Add inventory API handlers by delegating to Task 4

**Files:**
- Modify: `reply_server.py`
- Test: `tests/test_delivery_config_inventory_api.py`

- [ ] **Step 1: Add common inventory scope and `CardInventoryError` mapping**

  Reuse the same account/card ownership checks for every endpoint. Map stable Task 4 codes to safe HTTP statuses/details.

- [ ] **Step 2: Add settings, summary, import, generate, and masked preview handlers**

  Pass validated data to `CardInventoryService`; return only counts, deficit, source/status counts, and masked values. Never call the Task 4 private decryption methods from an HTTP response path.

- [ ] **Step 3: Run inventory API tests and Task 4 regression tests**

  ```powershell
  .\venv\Scripts\python.exe -m pytest -q tests/test_delivery_config_inventory_api.py -k inventory tests/test_card_inventory_service.py tests/test_card_inventory_migration.py
  ```

### Task 5: Preserve `/setup/status` delivery summary compatibility

**Files:**
- Modify: `reply_server.py`
- Test: `tests/test_delivery_config_inventory_api.py`, existing guided setup tests

- [ ] **Step 1: Extend `_get_guided_delivery_summary` with the scoped config count**

  Keep `template_count` and existing configured detection. Treat a saved Task 5 config as configured without exposing its contents.

- [ ] **Step 2: Run setup compatibility tests**

  ```powershell
  .\venv\Scripts\python.exe -m pytest -q tests/test_delivery_config_inventory_api.py tests/test_guided_setup_service.py tests/test_guided_setup_api.py
  ```

### Task 6: Verify the repository and commit the implementation

**Files:**
- Modify: `delivery_config_service.py`, `db_manager.py`, `reply_server.py`
- Test: `tests/test_delivery_config_inventory_api.py`

- [ ] **Step 1: Run Task 5 targeted tests**

  ```powershell
  .\venv\Scripts\python.exe -m pytest -q tests/test_delivery_config_inventory_api.py
  ```

- [ ] **Step 2: Run related and full verification**

  ```powershell
  .\venv\Scripts\python.exe -m pytest -q tests/test_card_inventory*.py tests/test_guided_setup*.py tests/test_republish_api.py
  .\venv\Scripts\python.exe -m pytest -q
  .\venv\Scripts\python.exe -m py_compile delivery_config_service.py card_inventory_service.py db_manager.py reply_server.py tests/test_delivery_config_inventory_api.py
  git diff --check
  git status --short
  ```

  No frontend files are changed, so `node --check` is not applicable; if an accidental frontend edit appears, run it before committing.

- [ ] **Step 3: Review the diff for scope and secret leakage**

  Confirm no Task 6 provider call, Task 8 UI, Task 9 listing logic, cleartext card, URL, token, or provider key is added to logs or responses.

- [ ] **Step 4: Commit the implementation**

  ```powershell
  git add delivery_config_service.py db_manager.py reply_server.py tests/test_delivery_config_inventory_api.py
  git commit -m "feat: add delivery config and card inventory APIs"
  ```
