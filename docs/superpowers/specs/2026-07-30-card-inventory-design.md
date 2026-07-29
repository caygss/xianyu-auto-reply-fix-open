# 本地卡密库存与自动生成服务设计

## 目标

为单用户、单电脑、单安装目录的本地部署增加可调用的卡密库存领域模型与服务。服务负责保存、导入、随机生成、库存补充、按购买数量原子预占、提交发出和释放预占；不负责卡密兑换或第三方有效性验证，也不实现 API、适配器或 UI。

## 已确认的边界

- 商品作用域使用现有 `cards.id`，通过 `card_id` 标识。
- 账号作用域使用订单已有的 `cookie_id`，在库存领域模型中以 `account_id` 保存。
- 数据继续存放在现有 `DBManager` 管理的本地 SQLite 数据库中，不引入远程服务或多租户共享存储。
- `stock_ceiling` 限制未发出库存，即 `available + reserved`；`sent` 和 `invalidated` 不占用库存。
- 手工导入整批检查上限；若导入后会超过上限，则整批拒绝，不产生部分导入。
- Task 5 API、Task 6 适配器、Task 8 UI 不在本次实现范围。

## 方案

在 `DBManager.init_db()` 的现有初始化/迁移事务中增加三张表，由新的 `card_inventory_service.py` 通过注入的 `DBManager` 使用同一 SQLite 连接和锁执行领域操作。独立服务只暴露业务方法，不改变现有订单处理入口。

使用三张表而不是把预占批次隐含在逐张记录中：逐张记录保存真实库存状态，预占表保存订单级原子批次及幂等状态，设置表保存商品+账号的库存和生成策略。所有状态变更只允许在事务内完成。

## 持久化模型

### `card_inventory_items`

字段：

- `id INTEGER PRIMARY KEY`
- `user_id INTEGER NOT NULL`
- `card_id INTEGER NOT NULL`
- `account_id TEXT NOT NULL`：对应订单 `cookie_id`
- `secret_text TEXT NOT NULL`：使用现有敏感字段加密能力保存，领域返回时才解密
- `secret_digest TEXT NOT NULL`：使用本地密钥计算的不可逆去重摘要，不在日志输出
- `source_type TEXT NOT NULL`：`manual` 或 `generated`
- `status TEXT NOT NULL`：`available`、`reserved`、`sent`、`invalidated`
- `order_id TEXT`、`reservation_id TEXT`、`unit_index INTEGER`
- `idempotency_key TEXT`：导入/生成或订单操作的幂等标识
- `created_at`、`updated_at`、`reserved_at`、`delivered_at`

唯一约束为 `(user_id, card_id, account_id, secret_digest)`，防止同一商品/账号作用域重复卡密；不同作用域可以保存相同文本。普通查询只返回数量和脱敏信息。

### `card_inventory_settings`

使用 `(user_id, card_id, account_id)` 唯一作用域，保存：

- `stock_ceiling INTEGER NOT NULL DEFAULT 100`
- `low_stock_threshold INTEGER NOT NULL DEFAULT 20`
- `auto_replenish INTEGER NOT NULL DEFAULT 0`
- `generator_prefix TEXT`
- `generator_length INTEGER NOT NULL DEFAULT 16`
- `generator_charset TEXT NOT NULL`
- `updated_at`

设置校验拒绝零或负库存上限、负预警线、过短/过长生成长度和空字符集。默认生成字符集使用安全随机字符，并排除易混淆字符 `I`、`O`、`0`、`1`。

### `card_inventory_reservations`

字段：

- `reservation_id TEXT PRIMARY KEY`
- `user_id INTEGER NOT NULL`
- `card_id INTEGER NOT NULL`
- `account_id TEXT NOT NULL`
- `order_id TEXT NOT NULL`
- `quantity INTEGER NOT NULL`
- `status TEXT NOT NULL`：`reserved`、`committed`、`released`
- `idempotency_key TEXT`
- `created_at`、`updated_at`、`committed_at`、`released_at`

同一 `(user_id, card_id, account_id, order_id)` 只能存在一个有效预占批次；幂等键也在作用域内唯一。预占批次中的 N 条 item 具有相同 `reservation_id`，并按 `unit_index` 稳定返回。

迁移使用 `CREATE TABLE IF NOT EXISTS` 与索引创建，重复启动安全，不修改或删除现有 `cards`、`orders`、`delivery_rules`、`data_card_reservations` 数据。

## 服务接口与行为

`CardInventoryService` 接受 `DBManager` 实例；所有公开方法都要求 `user_id`、`card_id`、`account_id`，调用方不能跨作用域访问。

- `save_settings(...)`：创建或更新商品+账号库存设置。
- `import_items(..., secrets, idempotency_key=None)`：逐条规范化输入，忽略空行，重复文本计入重复数；先计算整批所需槽位，超出 `stock_ceiling - (available + reserved)` 时整体失败，不写入任何新卡密。
- `generate_items(...)`：依据当前 `available + reserved` 补到 `stock_ceiling`，只生成缺口数量；每张使用系统安全随机源，生成前缀、长度和字符集来自设置；随机冲突时重试，数据库唯一约束是最终保护。
- `get_inventory_summary(...)`：返回 `available`、`reserved`、`sent`、`invalidated`、`total`、`stock_ceiling` 等数量，不返回完整卡密。
- `reserve_items(..., order_id, quantity, idempotency_key=None)`：数量必须为正整数。在同一 `BEGIN IMMEDIATE` 事务中先复用相同订单/幂等键的既有结果，再检查可用数量，一次性选出 N 张不同 `available` item 并改为 `reserved`。不足时返回 `insufficient_inventory`，不写任何部分预占。
- `commit_reservation(reservation_id, user_id, card_id, account_id)`：先校验预占记录属于完整的商品+账号作用域，再将该批次和全部 item 从 `reserved` 改为 `sent`，记录发出时间并返回按单元排序的卡密文本；已提交的重复调用复用相同结果，不重复扣减。
- `release_reservation(reservation_id, user_id, card_id, account_id)`：先校验完整的商品+账号作用域，再将 `reserved` item 恢复为 `available` 并清理预占关联；已释放或已提交的重复调用返回现有终态，不重复修改。
- `replenish_generated_inventory(...)`：调用生成逻辑补齐当前缺口；只补 `source_type=generated` 的缺口，不改变手工导入卡密的来源。

库存和状态更新使用 `DBManager.lock` 与 SQLite 事务。并发 reserve 即使来自多个线程，也不能重复选择同一 item 或超过可用库存。订单重复回调、进程重启和 service 重建都从持久化 reservation 状态恢复幂等结果。

## 错误和日志

领域错误使用稳定错误码和可读消息，包括 `invalid_quantity`、`invalid_settings`、`inventory_ceiling_exceeded`、`insufficient_inventory`、`reservation_not_found`、`scope_mismatch` 和 `invalid_state_transition`。错误中只包含商品/账号标识、数量、订单号或需要补充数量，不包含卡密文本。

普通日志只记录操作类型、作用域、数量、订单号、reservation id 和结果状态。禁止记录明文卡密、加密密文、digest 或整个请求对象。卡密只有在成功 commit 的返回值中交给后续发货调用方。

## 测试策略

先写失败测试再写生产代码，每轮遵循 RED-GREEN-REFACTOR：

1. 迁移：新数据库和包含旧表的数据库都能创建三张表，重复初始化不丢现有数据。
2. 导入/生成：导入去重、空行、整批超上限回滚；生成不可预测、不重复、按缺口补充且不超过上限。
3. 库存状态：summary 计数准确；sent 不占库存；invalidated 不重新进入 available。
4. 原子预占：购买数量 N 得到 N 张不同卡密；库存不足不产生部分预占；commit 扣减库存并返回卡密；release 恢复库存。
5. 并发/幂等：并发 reserve 不超卖；重复订单和重复幂等键复用 reservation；commit/release 重复调用不重复改变状态；重启后仍能读取终态。
6. 安全：日志捕获中不出现完整卡密，普通摘要和错误不包含卡密文本。

本次只增加领域模型/service 与测试，不修改 API、适配器、订单发货入口或 UI。
