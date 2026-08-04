# Durable Delivery Send State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every configured external send durably non-reclaimable before sender invocation, distinguish proven completion from unknown outcomes, and validate the complete persisted claim payload.

**Architecture:** Extend the orchestration row with trusted `item_id`, one-shot `send_started_at`, and `verification_required`. A public `begin_send` method performs exact atomic validation and writes the durable barrier; runtime arbitration creates a normal sent finalization anchor only after the sender task has successfully completed.

**Tech Stack:** Python 3.12, asyncio, sqlite3, pytest.

---

### Task 1: Canonical and legacy schema

**Files:**
- Modify: `db_manager.py:737`
- Test: `tests/test_delivery_orchestration_migration.py`

- [ ] **Step 1: Write failing canonical and legacy SQLite tests**

Assert that new databases define `item_id TEXT`, `send_started_at TIMESTAMP`, and `verification_required INTEGER NOT NULL DEFAULT 0` in the CREATE statement without ALTER fallback. Build a historical orchestration/finalization database containing `claim_verification_required=true`, initialize `DBManager`, and assert all columns exist and the matching orchestration row has a non-null send-start timestamp plus `verification_required=1`.

- [ ] **Step 2: Run migration tests and verify RED**

Run: `venv\Scripts\python.exe -m pytest -q tests/test_delivery_orchestration_migration.py`

Expected: FAIL because the three columns and historical migration do not exist.

- [ ] **Step 3: Implement the transactional migration**

Add the columns to the canonical CREATE statement and use `ALTER TABLE` for missing historical columns inside `init_db`'s existing transaction. Parse historical finalization JSON in Python and update matching orchestration rows before the enclosing commit; allow any exception to reach the existing rollback.

- [ ] **Step 4: Run migration tests and verify GREEN**

Run the command from Step 2 and expect all tests to pass.

### Task 2: Durable begin-send barrier and exact payload validation

**Files:**
- Modify: `delivery_orchestration_service.py:155-805`
- Test: `tests/test_delivery_idempotency.py`

- [ ] **Step 1: Write failing service tests**

Using real SQLite, prepare a claim and assert:

- `begin_send` atomically sets `send_started_at` and `verification_required=1`;
- a second `begin_send` with the same token returns false;
- forced stale `claimed_at` cannot make `prepare_retry` return a fresh send after begin-send;
- mismatched quantity, mode, idempotency key, or item ID raises a token-free validation/concurrency error;
- `mark_failed` clears both durable fields and retry reuses the same reservation;
- `mark_sent` clears both fields terminally.

- [ ] **Step 2: Run focused tests and verify RED**

Run the new test node IDs with `venv\Scripts\python.exe -m pytest -q`.

Expected: FAIL because `begin_send`, persisted item scope, and durable fields are absent and stale reclaim still succeeds.

- [ ] **Step 3: Implement minimal service state-machine changes**

Extend row selects/results with the new fields, persist `item_id` on insert, and add public `begin_send(request, claim_token)`. Its single UPDATE must match state ID, `status='sending'`, token, persisted quantity/mode/idempotency/item scope, `send_started_at IS NULL`, and then set both durable fields. Exclude non-null `send_started_at` from stale reclaim. Clear durable fields in exact-token sent/failed transitions.

- [ ] **Step 4: Run focused and full service tests and verify GREEN**

Run: `venv\Scripts\python.exe -m pytest -q tests/test_delivery_idempotency.py tests/test_delivery_orchestration_migration.py`

### Task 3: Runtime preflight and sender/heartbeat arbitration

**Files:**
- Modify: `XianyuAutoAsync.py:3626-3930`
- Test: `tests/test_bound_delivery_finalization_runtime.py`

- [ ] **Step 1: Write failing runtime tests**

Cover real service state with only sender/storage boundaries injected:

- begin-send false and exception call sender zero times;
- after either preflight failure, lease expiry still cannot create a send if begin-send was committed;
- heartbeat failure while sender is unfinished yields verification-required, no sent finalization, no confirm/finalize, and no resend after lease expiry;
- caller cancellation has the same durable unknown semantics and propagates only `CancelledError`;
- heartbeat failure concurrent with a successfully completed sender creates a normal sent anchor and can recover without resend;
- begin-send DB work remains event-loop nonblocking.

- [ ] **Step 2: Run focused runtime nodes and verify RED**

Expected failures: sender starts without durable prewrite; unknown is persisted as sent; completed/unfinished arbitration is not distinguished.

- [ ] **Step 3: Implement runtime integration**

Replace the immediate renew preflight with `await asyncio.to_thread(service.begin_send, request, token)`. Create the sender task only after true. If heartbeat finishes first, inspect whether sender is already done: successful completion follows the sent-anchor path; unfinished/cancelled work remains verification-required and never writes status sent. Keep cancellation cleanup and token-free safety exceptions.

- [ ] **Step 4: Run target file and verify GREEN**

Run: `venv\Scripts\python.exe -m pytest -q tests/test_bound_delivery_finalization_runtime.py`

### Task 4: Recovery and four-entry fail-closed behavior

**Files:**
- Modify: `XianyuAutoAsync.py`
- Modify: `reply_server.py`
- Test: `tests/test_bound_delivery_finalization_runtime.py`
- Test: `tests/test_bound_item_delivery_runtime.py`

- [ ] **Step 1: Write failing recovery/entry tests**

Persist historical and new verification-required records, then invoke simplified, compensation, main-auto, and manual paths. Assert sender, mark_sent, platform confirm, and finalize are all zero; order remains non-shipped; returned/logged Chinese reason explains that manual verification is required and contains no token.

- [ ] **Step 2: Run focused tests and verify RED**

Expected: historical sent anchors are returned by `_get_pending_delivery_finalization_meta` and automatically finalized.

- [ ] **Step 3: Implement shared fail-closed disposition**

Make pending-finalization lookup reject historical `claim_verification_required`. Surface orchestration `verification_required` through `_result` and `_bound_delivery_result` as disposition `verification_required` with token-free Chinese reason. Add that disposition to the existing no-send/no-failure handling sets in all four entries.

- [ ] **Step 4: Run both runtime files and verify GREEN**

Run: `venv\Scripts\python.exe -m pytest -q tests/test_bound_delivery_finalization_runtime.py tests/test_bound_item_delivery_runtime.py`

### Task 5: Verification and commit

**Files:**
- Verify only the files changed by Tasks 1-4 and this plan/spec.

- [ ] **Step 1: Run delivery/binding related tests**

Run the established 11-file delivery/binding collection and require zero failures.

- [ ] **Step 2: Run the complete test suite**

Run: `venv\Scripts\python.exe -m pytest -q`

- [ ] **Step 3: Run static checks**

Run `py_compile` for changed Python files and tests, followed by `git diff --check`.

- [ ] **Step 4: Self-review security and scope**

Verify claim tokens exist only in orchestration token columns or `_orchestration_private.claim_token`, every configured entry calls the common wrapper, legacy `configured=False` bypasses orchestration, and `.superpowers/` is absent from staging.

- [ ] **Step 5: Commit**

Stage only intended files, force-add ignored target tests when required, and create a new commit without amending prior commits.
