# 本地卡密库存与自动生成服务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有本地 SQLite/`DBManager` 中增加按商品+账号隔离的卡密库存、随机生成和原子 reserve/commit/release 领域服务。

**Architecture:** 扩展 `DBManager.init_db()` 与 `_migrate_database()` 创建三张库存表；新增 `CardInventoryService` 作为唯一领域入口，复用 `DBManager` 的 SQLite 连接、`RLock` 和敏感字段加密能力。库存 item 保存加密文本，reservation 表保存订单级幂等批次，所有库存状态变更在 `BEGIN IMMEDIATE` 事务内完成。

**Tech Stack:** Python 3、SQLite 3、现有 `DBManager`、`cryptography.fernet`、`secrets`、pytest、`ThreadPoolExecutor`。

---

### Task 1: 为库存表迁移建立失败测试

**Files:**
- Create: `tests/test_card_inventory_migration.py`
- Read: `db_manager.py:20-75,279-290,1014-1025,1027-1035`

- [ ] **Step 1: 写新数据库迁移的失败测试**

```python
import sqlite3

from db_manager import DBManager


def test_new_database_creates_card_inventory_tables_and_indexes(tmp_path):
    manager = DBManager(str(tmp_path / "inventory.sqlite3"))

    with sqlite3.connect(manager.db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "card_inventory_items",
            "card_inventory_settings",
            "card_inventory_reservations",
        } <= tables
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(card_inventory_items)"
            )
        }
        assert {
            "user_id", "card_id", "account_id", "secret_text", "secret_digest",
            "source_type", "status", "order_id", "reservation_id",
            "unit_index", "idempotency_key", "created_at", "updated_at",
            "reserved_at", "delivered_at",
        } <= columns

    manager.close()
```

- [ ] **Step 2: 写旧数据库重复迁移的失败测试**

```python
def test_inventory_migration_is_idempotent_and_preserves_existing_tables(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    manager = DBManager(str(db_path))
    with manager.lock:
        manager.conn.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
        manager.conn.execute("INSERT INTO legacy_marker(value) VALUES ('keep')")
        manager.conn.commit()
        manager.init_db()
    manager.close()

    restarted = DBManager(str(db_path))
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT value FROM legacy_marker"
        ).fetchone() == ("keep",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'card_inventory_reservations'"
        ).fetchone() == ("card_inventory_reservations",)
    restarted.close()
```

- [ ] **Step 3: 运行迁移测试确认按预期失败**

Run: `python -m pytest -q tests/test_card_inventory_migration.py`

Expected: FAIL because the three inventory tables do not yet exist.

- [ ] **Step 4: 提交迁移测试**

```powershell
git add tests/test_card_inventory_migration.py
git commit -m "test: define card inventory schema contract"
```

### Task 2: 实现可重复的库存 schema 和设置基础

**Files:**
- Modify: `db_manager.py:488-730,1027-1035`
- Test: `tests/test_card_inventory_migration.py`
- Create: `card_inventory_service.py`
- Create: `tests/test_card_inventory_service.py`

- [ ] **Step 1: 在 `DBManager.init_db()` 创建三张表和索引**

创建如下约束：`card_inventory_items` 的唯一键为 `(user_id, card_id, account_id, secret_digest)`；`card_inventory_settings` 的唯一键为 `(user_id, card_id, account_id)`；`card_inventory_reservations` 的唯一键为 `(user_id, card_id, account_id, order_id)`，并对状态、来源、数量添加 `CHECK`。secret 文本列保存 `DBManager._encrypt_secret()` 的结果，禁止在 SQL 日志中拼接参数。

- [ ] **Step 2: 运行迁移测试确认通过**

Run: `python -m pytest -q tests/test_card_inventory_migration.py`

Expected: PASS.

- [ ] **Step 3: 写设置和作用域校验的失败测试**

```python
import pytest

from card_inventory_service import CardInventoryService, CardInventoryError
from db_manager import DBManager


@pytest.fixture
def inventory(tmp_path):
    manager = DBManager(str(tmp_path / "inventory.sqlite3"))
    yield CardInventoryService(manager), manager
    manager.close()


def test_save_settings_persists_per_account_scope(inventory):
    service, manager = inventory

    result = service.save_settings(
        card_id=7, user_id=1, account_id="cookie-a",
        stock_ceiling=3, low_stock_threshold=1, auto_replenish=True,
    )

    assert result["stock_ceiling"] == 3
    assert service.get_inventory_summary(7, 1, "cookie-a")["stock_ceiling"] == 3
    assert service.get_inventory_summary(7, 1, "cookie-b")["stock_ceiling"] == 100


def test_settings_reject_non_positive_ceiling(inventory):
    service, _ = inventory

    with pytest.raises(CardInventoryError, match="库存上限"):
        service.save_settings(7, 1, "cookie-a", stock_ceiling=0)
```

- [ ] **Step 4: 运行设置测试确认按预期失败**

Run: `python -m pytest -q tests/test_card_inventory_service.py -k settings`

Expected: FAIL because `CardInventoryService` and its settings methods do not exist.

- [ ] **Step 5: 实现最小 service 骨架和设置 CRUD**

`CardInventoryService` 构造函数接收 `DBManager`；设置方法统一要求 `user_id`, `card_id`, `account_id`，以 `(user_id, card_id, account_id)` 查询/更新。使用稳定错误码属性 `CardInventoryError.code` 和可读中文消息；summary 初始值返回 `available=0, reserved=0, sent=0, invalidated=0, total=0`。

- [ ] **Step 6: 运行设置测试确认通过**

Run: `python -m pytest -q tests/test_card_inventory_service.py -k settings`

Expected: PASS.

### Task 3: 实现手工导入、随机生成和库存摘要

**Files:**
- Modify: `card_inventory_service.py`
- Modify: `tests/test_card_inventory_service.py`

- [ ] **Step 1: 写手工导入失败测试**

```python
def test_import_deduplicates_blank_lines_and_encrypts_without_logging_secret(inventory, caplog):
    service, manager = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=3)

    result = service.import_items(7, 1, "cookie-a", [" secret-a ", "", "secret-a", "secret-b"])

    assert result["inserted"] == 2
    assert result["duplicates"] == 1
    assert result["blank"] == 1
    assert service.get_inventory_summary(7, 1, "cookie-a")["available"] == 2
    with manager.lock:
        stored = manager.conn.execute(
            "SELECT secret_text FROM card_inventory_items"
        ).fetchone()[0]
    assert stored != "secret-a"
    assert "secret-a" not in caplog.text


def test_import_over_ceiling_rejects_entire_batch(inventory):
    service, manager = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=2)
    service.import_items(7, 1, "cookie-a", ["secret-a"])

    with pytest.raises(CardInventoryError, match="库存上限") as error:
        service.import_items(7, 1, "cookie-a", ["secret-b", "secret-c"])

    assert error.value.code == "inventory_ceiling_exceeded"
    assert service.get_inventory_summary(7, 1, "cookie-a")["available"] == 1
```

- [ ] **Step 2: 运行导入测试确认失败**

Run: `python -m pytest -q tests/test_card_inventory_service.py -k import`

Expected: FAIL because import behavior is not implemented.

- [ ] **Step 3: 写随机生成和补充失败测试**

```python
def test_generate_fills_gap_with_unique_opaque_values(inventory):
    service, _ = inventory
    service.save_settings(
        7, 1, "cookie-a", stock_ceiling=8,
        generator_prefix="AC-", generator_length=20,
    )
    service.import_items(7, 1, "cookie-a", ["manual-secret"])

    result = service.generate_items(7, 1, "cookie-a")
    assert result["generated"] == 7
    assert service.get_inventory_summary(7, 1, "cookie-a")["available"] == 8
    with inventory[1].lock:
        rows = inventory[1].conn.execute(
            "SELECT secret_text FROM card_inventory_items WHERE card_id = 7"
        ).fetchall()
    values = [inventory[1]._decrypt_secret(row[0]) for row in rows]
    assert len(values) == len(set(values)) == 8
    assert all(value.startswith("AC-") for value in values if value != "manual-secret")
    assert all(len(value) >= 20 for value in values if value != "manual-secret")

```

- [ ] **Step 4: 运行生成测试确认失败**

Run: `python -m pytest -q tests/test_card_inventory_service.py -k generate`

Expected: FAIL because generation and reservation-backed sent counting are not implemented.

- [ ] **Step 5: 实现导入、摘要和安全随机生成**

规范化文本后先在事务内计算去重后的新增数量和 `available + reserved` 缺口；超上限整批 rollback。使用 `secrets.choice` 或 `secrets.token_urlsafe` 生成不透明值，过滤重复值；摘要只返回计数。测试辅助方法只返回测试中的脱敏/领域值，不得加入生产公开 API；如果不需要辅助方法，测试通过数据库解密读取并在测试中使用，不把完整 secret 放入日志。

- [ ] **Step 6: 运行导入/生成测试确认通过**

Run: `python -m pytest -q tests/test_card_inventory_service.py -k "import or generate"`

Expected: PASS.

### Task 4: 实现原子 reserve/commit/release 与幂等

**Files:**
- Modify: `card_inventory_service.py`
- Modify: `tests/test_card_inventory_service.py`

- [ ] **Step 1: 写数量校验和库存不足失败测试**

```python
def test_reserve_requires_positive_quantity_and_never_partially_reserves(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=5)
    service.import_items(7, 1, "cookie-a", ["a", "b"])

    with pytest.raises(CardInventoryError, match="购买数量"):
        service.reserve_items(7, 1, "cookie-a", "order-0", 0)
    with pytest.raises(CardInventoryError, match="库存不足") as error:
        service.reserve_items(7, 1, "cookie-a", "order-1", 3)

    assert error.value.code == "insufficient_inventory"
    assert service.get_inventory_summary(7, 1, "cookie-a")["available"] == 2


def test_reserve_commit_returns_n_distinct_values_and_decrements_available(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=5)
    service.import_items(7, 1, "cookie-a", ["a", "b", "c"])

    reservation = service.reserve_items(7, 1, "cookie-a", "order-1", 2)
    assert reservation["quantity"] == 2
    assert service.get_inventory_summary(7, 1, "cookie-a")["reserved"] == 2
    committed = service.commit_reservation(reservation["reservation_id"], 1, 7, "cookie-a")

    assert committed["status"] == "committed"
    assert len(committed["items"]) == 2
    assert len(set(committed["items"])) == 2
    assert service.get_inventory_summary(7, 1, "cookie-a")["sent"] == 2


def test_generate_does_not_count_sent_items_toward_ceiling(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=2)
    service.import_items(7, 1, "cookie-a", ["secret-a", "secret-b"])
    reservation = service.reserve_items(7, 1, "cookie-a", "order-1", 1)
    service.commit_reservation(reservation["reservation_id"], 1, 7, "cookie-a")

    assert service.generate_items(7, 1, "cookie-a")["generated"] == 1
    assert service.get_inventory_summary(7, 1, "cookie-a")["sent"] == 1
```

- [ ] **Step 2: 运行 reserve 测试确认失败**

Run: `python -m pytest -q tests/test_card_inventory_service.py -k reserve`

Expected: FAIL because reservation transitions are not implemented.

- [ ] **Step 3: 写 release、重复回调和跨作用域失败测试**

```python
def test_release_is_idempotent_and_scope_mismatch_cannot_commit(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=3)
    service.import_items(7, 1, "cookie-a", ["a", "b"])
    reservation = service.reserve_items(7, 1, "cookie-a", "order-1", 2)

    released = service.release_reservation(reservation["reservation_id"], 1, 7, "cookie-a")
    repeated = service.release_reservation(reservation["reservation_id"], 1, 7, "cookie-a")
    assert released["status"] == repeated["status"] == "released"
    assert service.get_inventory_summary(7, 1, "cookie-a")["available"] == 2

    with pytest.raises(CardInventoryError) as error:
        service.commit_reservation(reservation["reservation_id"], 1, 7, "cookie-b")
    assert error.value.code == "scope_mismatch"


def test_duplicate_order_reuses_reservation_and_commit_result(inventory):
    service, _ = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=3)
    service.import_items(7, 1, "cookie-a", ["a", "b"])

    first = service.reserve_items(7, 1, "cookie-a", "order-1", 1)
    second = service.reserve_items(7, 1, "cookie-a", "order-1", 1)
    assert second["reservation_id"] == first["reservation_id"]
    assert service.get_inventory_summary(7, 1, "cookie-a")["reserved"] == 1

    committed = service.commit_reservation(first["reservation_id"], 1, 7, "cookie-a")
    repeated = service.commit_reservation(first["reservation_id"], 1, 7, "cookie-a")
    assert repeated == committed
```

- [ ] **Step 4: 运行幂等测试确认失败**

Run: `python -m pytest -q tests/test_card_inventory_service.py -k "release or duplicate"`

Expected: FAIL because idempotent state handling is not implemented.

- [ ] **Step 5: 写并发不超卖失败测试**

```python
from concurrent.futures import ThreadPoolExecutor


def test_concurrent_reservations_cannot_oversell(inventory):
    service, manager = inventory
    service.save_settings(7, 1, "cookie-a", stock_ceiling=2)
    service.import_items(7, 1, "cookie-a", ["a", "b"])

    def reserve(order_id):
        try:
            return service.reserve_items(7, 1, "cookie-a", order_id, 2)
        except CardInventoryError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ["order-a", "order-b"]))

    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("insufficient_inventory") == 1
    assert service.get_inventory_summary(7, 1, "cookie-a")["reserved"] == 2
    manager.close()
```

- [ ] **Step 6: 运行并发测试确认失败**

Run: `python -m pytest -q tests/test_card_inventory_service.py -k concurrent`

Expected: FAIL until `BEGIN IMMEDIATE` and row selection/update are implemented atomically.

- [ ] **Step 7: 实现事务状态机与幂等键**

`reserve_items` 在 `DBManager.lock` 下执行 `BEGIN IMMEDIATE`，先查询相同作用域的 order/idempotency key，再 `SELECT ... WHERE status='available' LIMIT ?`，数量不足直接 rollback；成功后插入 reservation 并批量更新 item。commit/release 校验 reservation 所有权和账号作用域，在同一事务按 reservation id 更新全部 item，再更新 reservation；重复终态返回持久化结果。任何异常 rollback，错误消息不携带 secret。

- [ ] **Step 8: 运行全部 service 测试确认通过**

Run: `python -m pytest -q tests/test_card_inventory_service.py`

Expected: PASS.

### Task 5: 回归验证、安全审查与提交

**Files:**
- Modify: `card_inventory_service.py`
- Modify: `db_manager.py`
- Test: `tests/test_card_inventory_migration.py`
- Test: `tests/test_card_inventory_service.py`

- [ ] **Step 1: 补充日志和普通返回值不泄露测试**

捕获 `loguru` 输出或注入测试 sink，执行 import、generate、reserve、commit 和 release；断言明文 secret、加密密文和 digest 不出现在普通日志、summary、错误文本中，只有 commit 返回的 `items` 包含发货文本。

- [ ] **Step 2: 运行定向测试**

Run: `python -m pytest -q tests/test_card_inventory_migration.py tests/test_card_inventory_service.py`

Expected: PASS.

- [ ] **Step 3: 运行相关测试**

Run: `python -m pytest -q tests/test_db_manager_logging.py tests/test_distribution_contract.py tests/test_delivery_republish_hook.py tests/test_republish_store.py`

Expected: PASS with no changes to unrelated behavior.

- [ ] **Step 4: 运行全量 pytest、py_compile 和 diff 检查**

```powershell
python -m pytest -q
python -m py_compile db_manager.py card_inventory_service.py tests/test_card_inventory_migration.py tests/test_card_inventory_service.py
git diff --check
```

Expected: all commands exit 0; pytest reports zero failures; `git diff --check` has no output.

- [ ] **Step 5: 检查 Task 4 范围和工作区差异**

Run: `git status --short` and `git diff --stat HEAD~2..HEAD`.

Expected: only the committed Task 4 service/model/tests plus the design/plan commits are included; pre-existing `.superpowers/` artifacts remain untouched and uncommitted.

- [ ] **Step 6: 提交实现**

```powershell
git add db_manager.py card_inventory_service.py tests/test_card_inventory_migration.py tests/test_card_inventory_service.py
git commit -m "feat: add local card inventory service"
```
