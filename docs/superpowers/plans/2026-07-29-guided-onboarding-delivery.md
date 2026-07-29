# Guided Onboarding and Multi-Channel Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将首次登录、账号恢复、商品交付配置、网盘/卡密发货、库存保护和自动重新上架整合成适合零基础 Windows 用户的中文引导流程。

**Architecture:** 在现有 FastAPI、SQLite、`reply_server.py`、`db_manager.py` 和 `static/js/app.js` 基础上增加两个聚焦服务：`guided_setup_service.py` 负责把内部状态转换为可执行的用户任务，`card_inventory_service.py` 负责逐张卡密的库存、锁定、发出和补充。现有卡券与发货规则保留为兼容层，订单处理通过统一交付解析器选择网盘、库存卡密或第三方 API。

**Tech Stack:** Python 3、FastAPI、SQLite、aiohttp、现有 asyncio 任务、原生 JavaScript、Bootstrap、pytest、Node `--check`。

---

## 当前代码边界

实施时遵循现有结构，不进行无关重构：

- `db_manager.py\)：SQLite 建表、迁移、卡券/发货规则/发货日志/最终确认状态。
- `reply_server.py\)：账号运行态、卡券 API、发货 API、商品发布和重新上架相关 HTTP 路由。
- `XianyuAutoAsync.py\)：WebSocket、Token 恢复、自动 Cookie 刷新和订单消息处理。
- `static/index.html\)：账号管理、商品、卡券、自动发货、订单和发布页面骨架。
- `static/js/app.js\)：页面加载、账号诊断、卡券/规则交互、发货与发布交互。
- `republish_service.py`、`republish_scheduler.py`、`republish_store.py`：已有原商品重新上架链路，向导只负责配置和健康检查，不复制重新上架逻辑。
- `tests/`：已有 API、数据库、发货和重新上架回归测试；新增测试按服务边界拆分。

每个任务先写失败测试、再写最小实现、运行测试、最后小提交。项目生成的卡密只是一串交付文本，不实现兑换或有效性验证。闲鱼平台已经产生的风控冷却不强行取消，只缩短项目自身无意义等待并实时显示剩余时间。

### Task 1: 建立向导状态与用户动作契约

**Files:**
- Create: `guided_setup_service.py`
- Modify: `reply_server.py`
- Test: `tests/test_guided_setup_service.py`
- Test: `tests/test_guided_setup_api_contract.py`

- [ ] **Step 1: 写失败测试，固定状态对象和用户动作**

在 `tests/test_guided_setup_service.py` 测试 `build_guided_status()` 返回 `step_id`、`step_index`、`step_total`、`title`、`message`、`needs_user_action`、`primary_action`、`retry_at`、`remaining_seconds`、`technical_status`、`technical_detail`。覆盖 `reconnecting`、`qr_login_grace_wait`、`password_login_backoff_wait`、`verification_pending_manual`、`connected`。

在 `tests/test_guided_setup_api_contract.py` 固定接口：`GET /setup/status`、`POST /setup/action`、`POST /cookies/{cid}/manual-verification/open`、`POST /cookies/{cid}/manual-verification/complete`。测试未登录 401、已登录 JSON 返回和敏感字段不泄漏。

- [ ] **Step 2: 运行测试确认失败**

运行：

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_guided_setup_service.py tests/test_guided_setup_api_contract.py
~~~

预期：FAIL，提示向导服务或接口尚不存在。

- [ ] **Step 3: 实现纯状态转换服务**

在 `guided_setup_service.py` 实现：

~~~python
def build_guided_status(runtime_status, account_details=None, delivery_summary=None):
    """把内部运行态转换为唯一用户任务。"""

def get_user_action_for_runtime(runtime_status):
    """返回 action、title、message、needs_user_action。"""

def format_remaining_seconds(deadline, now=None):
    """返回不小于 0 的动态剩余秒数。"""
~~~

扫码稳定期显示“当前无需操作”；需要验证显示“打开验证页面”或“我已完成验证”；平台冷却显示“请等待”；连接稳定且交付配置完整显示“可以等待买家下单”。技术枚举只放在技术详情。

- [ ] **Step 4: 增加 FastAPI 接口**

在 `reply_server.py` 中让 `GET /setup/status` 复用 `/cookies/details` 和 `_build_live_runtime_status()`，再交给 `build_guided_status()`。`POST /setup/action` 只接受 `refresh_status`、`open_manual_verification`、`complete_manual_verification`、`go_to_delivery_config`、`finish`；未知动作返回 400。账号权限沿用 `_ensure_cookie_access()`。

- [ ] **Step 5: 运行测试并提交**

运行：

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_guided_setup_service.py tests/test_guided_setup_api_contract.py
~~~

预期：全部 PASS。

提交：

~~~powershell
git add guided_setup_service.py reply_server.py tests/test_guided_setup_service.py tests/test_guided_setup_api_contract.py
git commit -m "feat: add guided setup status contract"
~~~

### Task 2: 修复动态退避时间与首次人工验证优先级

**Files:**
- Modify: `XianyuAutoAsync.py:801-852, 7690-7767, 13109-13160, 13936-13986, 17427-17606`
- Modify: `reply_server.py:4168-4423`
- Modify: `static/js/app.js:960-999, 4809-4833`
- Test: `tests/test_runtime_backoff_contract.py`
- Test: `tests/test_manual_verification_contract.py`

- [ ] **Step 1: 写失败测试**

构造 `slider_failed` 截止时间，连续两次调用运行态构建函数，第二次的 `token_refresh_remaining_seconds` 必须小于第一次；过期后返回 0 且可重新尝试。测试第一次需要人工验证时返回 `open_manual_verification`，不得继续创建同一场景的自动重试任务。

- [ ] **Step 2: 运行失败测试**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_runtime_backoff_contract.py tests/test_manual_verification_contract.py
~~~

预期：FAIL，因为当前运行态只返回固定的 `last_token_refresh_error_message`。

- [ ] **Step 3: 给运行态增加动态退避字段**

在 `_build_live_runtime_status()` 返回：

~~~json
{
  "token_refresh_backoff_reason": "slider_failed",
  "token_refresh_backoff_until": 0,
  "token_refresh_remaining_seconds": 0,
  "token_refresh_can_retry": false,
  "user_action": "wait_backoff"
}
~~~

从 `XianyuLive.get_password_login_failure_backoff(cleaned_cid)` 计算剩余时间，普通用户只看到转换后的中文原因。

- [ ] **Step 4: 让后台循环只做一次人工介入转交**

在 `_try_password_login_refresh()` 的滑块/验证码失败分支中记录可接管场景，停止同一场景的自动递归尝试。保留已有 600 秒平台保护退避，但退避期间不启动新浏览器。人工验证完成后清理对应退避并立即重新检查 Token。

- [ ] **Step 5: 更新前端倒计时和提示**

`static/js/app.js` 使用截止时间每秒更新，刷新页面后从服务端截止时间重新计算。将“登录恢复退避中，暂不可接管”改为“自动验证失败，当前需要等待 X 分钟；等待结束后可重新打开验证页面”。仅后端标记有活动浏览器时显示接管按钮。

- [ ] **Step 6: 运行测试并提交**

运行：

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_runtime_backoff_contract.py tests/test_manual_verification_contract.py
node --check static/js/app.js
~~~

提交：

~~~powershell
git add XianyuAutoAsync.py reply_server.py static/js/app.js tests/test_runtime_backoff_contract.py tests/test_manual_verification_contract.py
git commit -m "fix: expose actionable auth recovery state"
~~~

### Task 3: 增加账号向导 UI 和持久化引导入口

**Files:**
- Modify: `static/index.html`
- Create: `static/css/guided-setup.css`
- Modify: `static/js/app.js`
- Test: `tests/test_guided_setup_ui_contract.py`

- [ ] **Step 1: 写前端契约测试**

固定 DOM ID：`guidedSetupPanel`、`guidedSetupStep`、`guidedSetupTitle`、`guidedSetupMessage`、`guidedSetupPrimaryAction`、`guidedSetupCountdown`、`guidedSetupTechnicalDetails`。固定函数：`renderGuidedSetupStatus()`、`loadGuidedSetupStatus()`、`handleGuidedSetupAction()`。普通标题不得出现 WebSocket、Token、鉴权。

- [ ] **Step 2: 运行失败测试**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_guided_setup_ui_contract.py
~~~

预期：FAIL。

- [ ] **Step 3: 添加向导容器和样式**

在账号管理页面顶部添加可关闭但可重新打开的向导卡片。等待状态只显示“当前无需操作”；错误状态只显示一个恢复动作；技术详情用 Bootstrap collapse 折叠。新增 CSS 适配 1366×768 Windows 窗口，不遮挡账号表格。

- [ ] **Step 4: 实现前端状态机**

`loadGuidedSetupStatus()` 每 3 秒刷新活动状态，每秒只更新倒计时；收到新的服务端截止时间后覆盖本地计时。动作完成后立即刷新。关闭只保存浏览器本地偏好，不删除服务端配置。

- [ ] **Step 5: 运行检查并提交**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_guided_setup_ui_contract.py
node --check static/js/app.js
~~~

~~~powershell
git add static/index.html static/css/guided-setup.css static/js/app.js tests/test_guided_setup_ui_contract.py
git commit -m "feat: add guided account setup UI"
~~~

### Task 4: 扩展卡密库存数据模型和逐张卡密服务

**Files:**
- Modify: `db_manager.py:490-725, 1039-1373, 1643-1686`
- Create: `card_inventory_service.py`
- Test: `tests/test_card_inventory_migration.py`
- Test: `tests/test_card_inventory_service.py`

- [ ] **Step 1: 写迁移和事务失败测试**

测试新安装和旧数据库都创建：

~~~sql
card_inventory_items(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  card_id INTEGER NOT NULL,
  secret_text TEXT NOT NULL,
  source_type TEXT NOT NULL,
  status TEXT NOT NULL,
  order_id TEXT,
  reservation_id TEXT,
  created_at TEXT NOT NULL,
  reserved_at TEXT,
  delivered_at TEXT,
  UNIQUE(user_id, card_id, secret_text)
)

card_inventory_settings(
  card_id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  stock_ceiling INTEGER NOT NULL DEFAULT 100,
  low_stock_threshold INTEGER NOT NULL DEFAULT 20,
  auto_replenish INTEGER NOT NULL DEFAULT 0,
  generator_prefix TEXT,
  generator_length INTEGER NOT NULL DEFAULT 16,
  generator_charset TEXT NOT NULL,
  updated_at TEXT NOT NULL
)
~~~

迁移必须可重复执行，已有 `cards`、`delivery_rules`、`data_card_reservations` 数据不得丢失。

- [ ] **Step 2: 运行失败测试**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_card_inventory_migration.py tests/test_card_inventory_service.py
~~~

预期：FAIL，因为新表或服务不存在。

- [ ] **Step 3: 实现迁移和索引**

在 `db_manager.py` 初始化迁移事务中创建表、用户索引和唯一约束。日志不得打印明文卡密。

- [ ] **Step 4: 实现库存服务接口**

在 `card_inventory_service.py` 实现：

~~~python
- `import_items(card_id: int, user_id: int, secrets: list[str]) -> dict`
- `generate_items(card_id: int, user_id: int, quantity: int, settings: dict) -> dict`
- `get_inventory_summary(card_id: int, user_id: int) -> dict`
- `reserve_items(card_id: int, user_id: int, order_id: str, quantity: int) -> dict`
- `commit_reservation(reservation_id: str, user_id: int) -> dict`
- `release_reservation(reservation_id: str, user_id: int) -> dict`
- `replenish_generated_inventory(card_id: int, user_id: int) -> dict`
~~~

`reserve_items()` 必须在同一 SQLite 事务中检查可用数量并一次性锁定全部数量；不足返回 `insufficient_inventory`；重复订单复用原预占记录。

- [ ] **Step 5: 实现随机生成和补充**

使用系统安全随机源，默认大写字母和数字并排除 `I`、`O`、`0`、`1`。库存启用后先生成到上限，低于预警线时后台补到上限。导入卡密只能由导入补充；第三方真实授权码不使用本地生成策略。

- [ ] **Step 6: 运行测试并提交**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_card_inventory_migration.py tests/test_card_inventory_service.py
~~~

~~~powershell
git add db_manager.py card_inventory_service.py tests/test_card_inventory_migration.py tests/test_card_inventory_service.py
git commit -m "feat: add quantity-aware card inventory"
~~~

### Task 5: 增加卡密导入、生成、库存和补充 API

**Files:**
- Modify: `reply_server.py:9562-9978`
- Modify: `db_manager.py`
- Test: `tests/test_card_inventory_api.py`

- [ ] **Step 1: 写 API 失败测试**

固定接口：`GET /cards/{card_id}/inventory`、`POST /cards/{card_id}/inventory/import`、`POST /cards/{card_id}/inventory/generate`、`POST /cards/{card_id}/inventory/replenish`、`POST /cards/{card_id}/inventory/export`。测试重复卡密、超过上限、跨用户访问、导出和库存不足。普通库存接口不返回全部明文。

- [ ] **Step 2: 运行失败测试**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_card_inventory_api.py
~~~

预期：FAIL，因为接口尚未注册。

- [ ] **Step 3: 实现 API 和用户隔离**

所有路由依赖 `get_current_user` 和 `get_card_by_id(card_id, user_id)`。导入接受 UTF-8 文本和 TXT/CSV 单列；生成路由只允许启用生成配置；错误返回稳定错误码和普通中文消息。

- [ ] **Step 4: 实现后台补充入口**

在现有 FastAPI 生命周期任务中增加每 60 秒检查一次的低频补充任务。每个 `card_id` 同时只允许一个补充任务；失败只更新脱敏状态，不阻塞订单。

- [ ] **Step 5: 运行测试并提交**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_card_inventory_api.py
~~~

~~~powershell
git add reply_server.py db_manager.py tests/test_card_inventory_api.py
git commit -m "feat: add card inventory APIs"
~~~

### Task 6: 统一交付解析和第三方发卡适配器

**Files:**
- Create: `delivery_adapters.py`
- Modify: `db_manager.py:647-725`
- Modify: `reply_server.py`
- Test: `tests/test_delivery_adapters.py`
- Test: `tests/test_item_delivery_resolution.py`

- [ ] **Step 1: 写失败测试，固定交付优先级**

测试优先级：商品专属配置 > SKU/规格配置 > 现有关键字发货规则 > 默认交付配置。每个适配器都提供 `validate_config()`、`reserve(quantity, order_context)`、`commit(reservation)`、`release(reservation)`。

- [ ] **Step 2: 运行失败测试**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_delivery_adapters.py tests/test_item_delivery_resolution.py
~~~

预期：FAIL。

- [ ] **Step 3: 实现网盘和本地卡密适配器**

网盘 `reserve()` 返回固定内容，不扣库存；本地卡密调用 `reserve_items()`，不足抛出结构化错误；提交和释放操作必须可重复调用。

- [ ] **Step 4: 实现第三方 API 适配器**

使用 `aiohttp`、超时和脱敏日志。请求数量与返回卡密数量必须相等，否则整单预占失败。网络异常、认证失败、数量不足分别映射为用户消息。

- [ ] **Step 5: 增加默认和商品专属配置 API**

新增 `item_delivery_configs`，按 `user_id + item_id` 唯一。接口：`GET /delivery/config`、`PUT /delivery/config/default`、`PUT /items/{item_id}/delivery-config`、`DELETE /items/{item_id}/delivery-config`。第三方密钥不出现在普通列表和日志。

- [ ] **Step 6: 运行测试并提交**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_delivery_adapters.py tests/test_item_delivery_resolution.py
~~~

~~~powershell
git add delivery_adapters.py db_manager.py reply_server.py tests/test_delivery_adapters.py tests/test_item_delivery_resolution.py
git commit -m "feat: add multi-channel delivery adapters"
~~~

### Task 7: 接入订单数量、整单暂停和幂等发货

**Files:**
- Modify: `reply_server.py:14700-15120`
- Modify: `db_manager.py:5326-5390, 5823-6143`
- Modify: `XianyuAutoAsync.py`
- Test: `tests/test_delivery_quantity_contract.py`
- Test: `tests/test_delivery_idempotency.py`

- [ ] **Step 1: 写数量失败测试**

购买数量 1、3、0、负数和缺失分别测试。数量 N 必须预占 N 个不同卡密；库存少于 N 返回 `insufficient_inventory`、订单状态 `pending_delivery`，不发送部分内容。

- [ ] **Step 2: 写重复通知测试**

同一个 `order_id + item_id + buyer_id` 连续处理两次，第二次复用第一次最终结果，不再次预占、发送或扣库存；进程重启后从 `delivery_finalization_states` 恢复同样行为。

- [ ] **Step 3: 运行失败测试**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_delivery_quantity_contract.py tests/test_delivery_idempotency.py
~~~

预期：新增测试 FAIL。

- [ ] **Step 4: 实现数量归一化和整单预占**

订单入口先归一化购买数量。卡密和第三方适配器收到数量 N；网盘默认发送一份固定链接并在日志记录购买数量。卡密必须一次性锁定 N 个。

- [ ] **Step 5: 实现提交、释放和补库存重试**

成功后提交全部预占；发送异常释放全部预占；库存不足写入待处理原因和需要补充数量。补充后提供“继续处理”接口，只重试原订单。

- [ ] **Step 6: 运行测试并提交**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_delivery_quantity_contract.py tests/test_delivery_idempotency.py
~~~

~~~powershell
git add reply_server.py db_manager.py XianyuAutoAsync.py tests/test_delivery_quantity_contract.py tests/test_delivery_idempotency.py
git commit -m "feat: make delivery quantity-aware and idempotent"
~~~

### Task 8: 商品配置和卡密库存 UI

**Files:**
- Modify: `static/index.html`
- Create: `static/css/delivery-config.css`
- Modify: `static/js/app.js`
- Test: `tests/test_delivery_config_ui_contract.py`

- [ ] **Step 1: 写 UI 契约测试**

固定 DOM ID：`deliveryConfigPrompt`、`deliveryConfigPanel`、`deliveryMethodSelect`、`defaultDeliveryContent`、`cardInventorySummary`、`cardImportInput`、`cardGenerateForm`、`cardReplenishButton`。测试关闭、缩放、重新打开和无技术术语提示。

- [ ] **Step 2: 运行失败测试**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_delivery_config_ui_contract.py
~~~

预期：FAIL。

- [ ] **Step 3: 添加常驻配置面板**

商品管理页面加入可关闭、可缩放、可重新打开的配置提示。默认交付与商品专属覆盖分开显示，支持网盘、卡密库存、自动生成和第三方 API。

- [ ] **Step 4: 添加库存管理交互**

显示上限、可用、锁定、已发出、预警线和自动补充状态。生成表单提供数量、前缀、长度、字符集和批次；导入提供文本和 TXT/CSV；导出二次确认。

- [ ] **Step 5: 添加商品专属配置和库存不足入口**

商品行提供“设置交付方式”；待处理订单显示“库存不足，还需要 X 个”和“去补充库存/继续处理”。不显示可能误解为部分发货的内容。

- [ ] **Step 6: 运行检查并提交**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_delivery_config_ui_contract.py
node --check static/js/app.js
~~~

~~~powershell
git add static/index.html static/css/delivery-config.css static/js/app.js tests/test_delivery_config_ui_contract.py
git commit -m "feat: add persistent delivery configuration UI"
~~~

### Task 9: 把向导接入商品、自动发货和重新上架

**Files:**
- Modify: `guided_setup_service.py`
- Modify: `reply_server.py`
- Modify: `static/index.html`
- Modify: `static/js/app.js`
- Test: `tests/test_guided_setup_completion.py`
- Test: `tests/test_delivery_republish_hook.py`

- [ ] **Step 1: 写完成条件测试**

测试未配置账号、未绑定商品、缺少默认交付内容、自动发货关闭、重新上架配置错误时，向导指出第一个阻塞项和对应按钮；全部满足时返回 `ready_to_wait_for_order`。

- [ ] **Step 2: 运行失败测试**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_guided_setup_completion.py tests/test_delivery_republish_hook.py
~~~

预期：新增测试 FAIL，已有重新上架回归测试不得失败。

- [ ] **Step 3: 实现配置检查聚合**

向导聚合账号运行态、默认交付配置、商品覆盖、自动发货状态和现有重新上架配置检查，只返回用户可执行的第一项阻塞任务，同时保留完整检查清单。

- [ ] **Step 4: 接入现有重新上架逻辑**

只调用现有重新上架服务，不复制发布代码。原商品重新上架作为默认模式；原商品不可用时显示“重新发布商品”并跳转发布页面。

- [ ] **Step 5: 运行测试并提交**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q tests/test_guided_setup_completion.py tests/test_delivery_republish_hook.py
~~~

~~~powershell
git add guided_setup_service.py reply_server.py static/index.html static/js/app.js tests/test_guided_setup_completion.py tests/test_delivery_republish_hook.py
git commit -m "feat: connect guided setup to delivery and republish"
~~~

### Task 10: 端到端回归、打包和分发文档

**Files:**
- Modify: `README.md`
- Modify: `docs/windows-distribution.md`
- Modify: `docs/windows-republish-runbook.md`
- Modify: `docs/usage.md`
- Test: `tests/test_guided_setup_e2e_contract.py`
- Test: `tests/test_distribution_contract.py`

- [ ] **Step 1: 写端到端契约测试**

覆盖：默认用户登录 → 扫码状态 → 稳定期倒计时 → 人工验证完成 → 账号可用 → 设置网盘默认内容 → 创建生成卡源 → 生成 100 个库存 → 购买数量 3 → 预占/提交 3 个 → 库存变 97 → 重复通知不重复发货 → 库存不足整单暂停 → 补库存后继续处理 → 重新上架配置通过。

- [ ] **Step 2: 运行完整测试**

~~~powershell
.\venv\Scripts\python.exe -m pytest -q
node --check static/js/app.js
git diff --check
~~~

预期：全部 PASS；允许已有 FastAPI `on_event` 弃用警告，但不得新增失败。

- [ ] **Step 3: 更新用户文档**

用普通中文说明扫码后的每一步、等待与人工验证、卡密仅负责发货不负责验证、库存不足整单暂停、网盘/卡密配置、自动重新上架。保留 AGPL-3.0 和非官方闲鱼工具说明。

- [ ] **Step 4: 构建 Windows 分发包**

使用当前提交的实际标签和哈希构建：

~~~powershell
$sourceTag = (git describe --tags --always HEAD)
$sourceCommit = (git rev-parse HEAD)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\build_windows_distribution.ps1 -SourceTag $sourceTag -SourceCommit $sourceCommit -ModificationDate 2026-07-29
~~~

构建后验证入口 EXE 可启动、`/health` 返回 healthy、向导静态资源存在、压缩包没有 `data/`、`browser_data/`、`logs/` 或本地数据库。

- [ ] **Step 5: 最终状态和提交**

运行：

~~~powershell
git status --short
git log --oneline -5
~~~

预期：工作区干净，记录 ZIP 和 EXE 的 SHA-256。

~~~powershell
git add README.md docs/windows-distribution.md docs/windows-republish-runbook.md docs/usage.md tests/test_guided_setup_e2e_contract.py tests/test_distribution_contract.py
git commit -m "docs: document guided onboarding and delivery workflow"
~~~

## 计划自检

### 规格覆盖

- 账号向导和用户可见状态：Task 1-3、Task 9。
- 动态倒计时和首轮人工验证：Task 2。
- 网盘固定链接：Task 6、Task 8。
- 卡密导入、第三方 API 和项目生成：Task 4-6、Task 8。
- 库存上限、当前库存、自动补充：Task 4-5。
- 按购买数量发货、整单暂停、重试幂等：Task 7。
- 商品配置常驻、关闭、缩放、重新打开：Task 8。
- 自动发货和原商品重新上架：Task 7、Task 9。
- Windows 分发、单机隔离、文档和验证：Task 10。

### 约束检查

- 项目不实现卡密兑换/验证：Task 6、Task 10。
- 不部分发货：Task 7 一次性预占并整单暂停。
- 不绕过闲鱼强制冷却：Task 2 动态展示和等待。
- 不复制已有重新上架逻辑：Task 9 调用现有服务。
- 不共享多个分发包的数据：Task 10 保留现有打包隔离规则。
