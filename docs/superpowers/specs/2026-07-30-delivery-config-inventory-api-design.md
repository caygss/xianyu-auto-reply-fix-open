# Task 5：交付配置与卡密库存 API 设计

## 目标与边界

为本地单用户、单电脑安装提供按“账号 + 商品卡片”隔离的交付配置 CRUD 和卡密库存管理 API。账号使用现有 `cookies.id`/`account_id`，商品使用现有 `cards.id`/`card_id`。本任务不调用第三方 provider，不改订单发货入口，不实现 UI 或上架流程。

## API 契约

所有新路由都依赖现有 `get_current_user` Bearer 鉴权，并同时校验当前用户拥有 `account_id` 和 `card_id`。`card_id` 使用路径参数，`account_id` 使用必填查询参数。

交付配置：

- `GET /api/cards/{card_id}/delivery-config?account_id=...`
- `PUT /api/cards/{card_id}/delivery-config?account_id=...`
- `DELETE /api/cards/{card_id}/delivery-config?account_id=...`
- 请求字段为 `{ "mode": "fixed_link|imported_card|generated_card|provider_api", "config": {...} }`。
- 返回只包含 `mode`、作用域标识和 `config_summary`，绝不返回原始 `config`。
- `fixed_link` 要求配置中的 `url` 为 `http`/`https`；其余模式要求非空对象。`provider_api` 仅保存配置，不执行网络请求。

库存：

- `GET/PUT /api/cards/{card_id}/inventory/settings?account_id=...`
- `GET /api/cards/{card_id}/inventory?account_id=...`
- `POST /api/cards/{card_id}/inventory/import?account_id=...`
- `POST /api/cards/{card_id}/inventory/generate?account_id=...`
- `GET /api/cards/{card_id}/inventory/preview?account_id=...`
- 设置接口只允许有效 `CardInventoryService.save_settings` 参数；导入/生成只返回数量、缺口和掩码摘要；preview 只返回掩码后的可用卡密预览，不返回明文。

## 存储与复用

新增 `item_delivery_configs` SQLite 表，以 `(user_id, card_id, account_id)` 唯一。整个配置 JSON 使用现有 DBManager 的 Fernet 能力加密保存，摘要在读取时从解密配置生成，敏感字段不进入日志或 HTTP 响应。库存 API 只调用 Task 4 的 `CardInventoryService`，不复制库存计数、导入去重、生成或事务逻辑。

`/setup/status` 继续保留原有模板 `delivery_summary` 字段与语义；在此基础上，若当前账号存在已保存的 Task 5 交付配置，也视为真实 configured，不返回配置内容。

## 错误与安全

空 `account_id`/`config`、非法 mode、非法链接、非法库存参数返回 400；账号或商品不属于当前用户返回 403；不存在的配置返回 404；Task 4 错误代码转换为中文 detail，并保留稳定 code 字段。日志只记录操作、作用域、数量和状态，不记录 URL 查询值、provider 密钥或完整卡密。

## 测试策略

先添加 API 路由存在、认证/作用域、四种 mode、校验、CRUD 脱敏、库存设置/导入/生成/统计/预览以及 setup status 兼容性契约测试，逐组确认 RED 后实现。完成后运行 Task 5 定向测试、Task 4 相关测试、全量 pytest、`py_compile`、前端未改动无需 node 检查，并检查 git diff 与敏感值泄漏。
