# Precise Delivery Item Scope Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make historical delivery item-scope migration owner-safe, sibling-isolated, linear in input size, and one-shot across database startups.

**Architecture:** Add a row-level `item_scope_migration_version` and partial index. During the first applicable startup, load each source table once, build ownership indexes in Python, resolve exact evidence before ambiguous evidence, and persist either a trusted item or a fail-closed marker. Later startups return before loading source tables when no old-version rows remain.

**Tech Stack:** Python 3.12, SQLite, pytest, existing `DBManager` migration savepoint.

---

### Task 1: Exact finalization ownership

**Files:**
- Modify: `tests/test_delivery_orchestration_migration.py`
- Modify: `db_manager.py`

- [ ] **Step 1: Write failing real-SQLite tests**

Add tests that seed legacy orchestration/finalization rows and assert:

```python
def test_historical_anchor_cannot_cross_account_with_forged_idempotency_key(tmp_path):
    # account-b/order-b anchor carries account-a's key.
    # account-a is not resolved or quarantined by that anchor.
    # account-b's unresolved row fails closed in its own scope.

def test_partial_or_multi_candidate_anchor_cannot_resolve_sibling_lines(tmp_path):
    # Two rows share account/order/card but have different order_line_id.
    # A card-only anchor resolves neither row; both remain item_id NULL and fail closed.
    # Original ambiguous delivery_meta is unchanged.

def test_complete_finalization_scope_uniquely_backfills_one_historical_row(tmp_path):
    # Same account/order plus card_id and order_line_id selects one row only.
    # The selected row gets item_id; its sibling is not resolved by that anchor.
```

- [ ] **Step 2: Run RED**

Run:

```powershell
venv\Scripts\python.exe -m pytest -q tests/test_delivery_orchestration_migration.py -k "cross_account or partial_or_multi or complete_finalization_scope"
```

Expected: failures showing cross-account key mutation and partial-scope reuse.

- [ ] **Step 3: Implement one-pass ownership indexes**

In `_migrate_delivery_orchestration_send_state`, load orchestration identities and finalizations once and construct:

```python
states_by_idempotency = {key: state}
states_by_account_order = defaultdict(list)
states_by_complete_scope = defaultdict(list)  # account, order, card, line
anchors_by_state_id = defaultdict(list)
ambiguous_candidate_state_ids = set()
```

Only assign an anchor when its own account/order matches and either exact key or complete card/line scope maps to exactly one state. Never use card-only, line-only, or singleton account/order fallback.

- [ ] **Step 4: Run GREEN**

Run the command from Step 2 and expect all selected tests to pass.

### Task 2: Sibling-safe quarantine

**Files:**
- Modify: `tests/test_delivery_orchestration_migration.py`
- Modify: `db_manager.py`

- [ ] **Step 1: Write failing two-startup sibling test**

```python
def test_unresolved_failed_sibling_does_not_quarantine_exact_sent_anchor_across_restarts(tmp_path):
    # A sent sibling has one exact anchor and trusted item.
    # A failed sibling has no exact anchor and cannot resolve.
    # Start DBManager twice; sent anchor metadata remains normal and sent returns noop.
    # Failed sibling alone is verification_required and retains reservation.
```

- [ ] **Step 2: Run RED**

Run the single test and expect the current account/order-wide metadata mutation to set `claim_verification_required` on the sent sibling anchor.

- [ ] **Step 3: Restrict finalization mutation to one exact owner**

Replace account/order-wide marking with a helper accepting the complete state identity. Merge verification fields only when `anchors_by_state_id[state_id]` contains exactly one exact anchor. Leave ambiguous/unowned anchor JSON unchanged.

- [ ] **Step 4: Run GREEN**

Run the single test and then all migration tests.

### Task 3: One-shot marker and linear startup

**Files:**
- Modify: `tests/test_delivery_orchestration_migration.py`
- Modify: `db_manager.py`

- [ ] **Step 1: Write failing schema and 10k-row tests**

Add canonical/upgrade assertions for:

```sql
item_scope_migration_version INTEGER NOT NULL DEFAULT 1
CREATE INDEX ... WHERE item_scope_migration_version < 1
```

Add a 10,000-row unresolved legacy database test. Trace only `_migrate_delivery_orchestration_send_state` during the second `DBManager` startup and assert no source-table loads (`delivery_finalization_states`, `orders`, `item_delivery_bindings`) occur, all rows have version 1, and the migration statement count stays within a small query-count bound independent of row count.

- [ ] **Step 2: Run RED**

Run marker/index/10k selected tests. Expect missing column/index and repeated source-table scans.

- [ ] **Step 3: Implement marker and bulk indexes**

Use canonical default 1 and upgraded-column default 0. Create the partial index inside the migration savepoint. Query old-version rows first; return before source loading when empty. Bulk-load orders and bindings once and index them in Python. After each resolved or isolated state update, set version 1.

- [ ] **Step 4: Run GREEN and refactor**

Run marker/scale tests, all migration tests, and `tests/test_delivery_idempotency.py`. Remove per-state SQL counts and all per-state full-list scans.

### Task 4: Regression verification and commit

**Files:**
- Modify: `docs/superpowers/specs/2026-08-04-durable-delivery-send-state-design.md`

- [ ] **Step 1: Run targeted and delivery/binding tests**

Run migration/idempotency tests and the established 11-file delivery/binding collection.

- [ ] **Step 2: Run full verification**

```powershell
venv\Scripts\python.exe -m pytest -q
venv\Scripts\python.exe -m py_compile db_manager.py delivery_orchestration_service.py XianyuAutoAsync.py
git diff --check
```

- [ ] **Step 3: Security and scope review**

Confirm no token appears in new reasons/metadata, ambiguous `delivery_meta` remains unchanged, reservation rows are retained, and `.superpowers/` is absent from the staged file list.

- [ ] **Step 4: Commit**

Stage only the migration, tests, design, and plan files and commit with:

```powershell
git commit -m "fix: make historical delivery migration owner-safe"
```
