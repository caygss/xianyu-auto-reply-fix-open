# Task 6 Delivery Adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接入四种交付渠道适配器，安全生成待发送内容，并为 Task 7 保留 reservation 驱动的订单发送接缝。

**Architecture:** `delivery_adapter_service.py` 定义 request、统一领域错误、四个 adapter、注入式 JSON transport 和 dispatcher。`delivery_config_service.py` 负责 provider 配置的保存期安全校验；`XianyuAutoAsync.py` 只增加可选 reservation seam，复用既有发送步骤，不实现 Task 7 状态策略。

**Tech Stack:** Python 3.12、pytest、标准库 `urllib`、现有 SQLite/Loguru 服务。

---

### Task 1: 配置与 dispatcher 契约测试

**Files:**
- Create: `tests/test_delivery_adapter_service.py`
- Modify: `tests/test_delivery_config_service.py` if a focused config test module is added by the repository

- [ ] **Step 1: 写失败测试**

覆盖 `DeliveryDispatcher.prepare` 的 fixed link、两种 card、provider fake transport；覆盖未知 mode、缺失配置、scope mismatch、provider endpoint/timeout/retry/response-limit 错误，以及 token/card secret 不进入日志或返回摘要。

- [ ] **Step 2: 运行 RED**

Run: `pytest tests/test_delivery_adapter_service.py -q`

Expected: FAIL because `delivery_adapter_service` and the requested dispatcher API do not yet exist.

### Task 2: provider 配置校验与统一 adapter 实现

**Files:**
- Create: `delivery_adapter_service.py`
- Modify: `delivery_config_service.py`
- Test: `tests/test_delivery_adapter_service.py`

- [ ] **Step 1: 实现最小公共 API**

定义 `DeliveryRequest`、`DeliveryDispatchError`、`ProviderResponse`、`JsonTransport`、`UrllibJsonTransport`、四个 adapter 和 `DeliveryDispatcher.prepare`。配置通过 `DeliveryConfigService.get_for_delivery` 仅在服务内部读取；卡密 adapter 仅调用上游 reservation 的完整作用域提交/读取，不创建 reservation。

- [ ] **Step 2: 加入 provider 安全边界**

校验 endpoint scheme/host/port/control characters，timeout 取 `1..30` 秒，max retries 取 `0..3`，响应最多读取 `64 KiB`；默认 Bearer token，headers 和字段映射只构建请求，不日志输出敏感值。

- [ ] **Step 3: 运行 GREEN 与相关测试**

Run: `pytest tests/test_delivery_adapter_service.py tests/test_card_inventory_service.py -q`

Expected: PASS，且 fake transport 的调用次数符合 retry 边界。

### Task 3: 接入既有自动发货发送接缝

**Files:**
- Modify: `XianyuAutoAsync.py`
- Test: `tests/test_delivery_adapter_service.py`

- [ ] **Step 1: 写失败回归测试**

实例化无网络的 `XianyuLive`，传入完整作用域和 reservation，断言 dispatcher 产物能转换为现有 text delivery step；未传 reservation 时断言旧链路不被改变。

- [ ] **Step 2: 实现最小接入**

增加可选 `_prepare_configured_delivery(...)` seam，使用 `DeliveryDispatcher` 和已有 `_build_delivery_steps`，不修改订单数量、幂等或整单暂停。

- [ ] **Step 3: 运行回归测试**

Run: `pytest tests/test_delivery_adapter_service.py tests/test_delivery_republish_hook.py -q`

Expected: PASS。

### Task 4: 全量验证与提交

**Files:**
- Verify all changed files with `git diff --check`

- [ ] **Step 1: 运行定向和全量 pytest**

Run: `pytest tests/test_delivery_adapter_service.py tests/test_delivery_config_service.py tests/test_card_inventory_service.py -q` followed by `pytest -q`.

- [ ] **Step 2: 运行编译检查**

Run: `python -m py_compile delivery_adapter_service.py delivery_config_service.py card_inventory_service.py XianyuAutoAsync.py reply_server.py`

- [ ] **Step 3: 检查差异并提交**

Run: `git diff --check` and `git status --short`; commit with `git add` only Task 6 files and `git commit -m "feat: add delivery channel adapters"`.
