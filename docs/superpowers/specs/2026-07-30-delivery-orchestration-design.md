# Task 7 Delivery Orchestration Design

## Goal

为订单自动发货增加一个可测试、按购买数量工作、整单暂停且幂等的编排服务，同时保留现有 `_auto_delivery`、消息发送和 Task 6 dispatcher 的兼容入口。

## Scope and non-goals

- 本次只修改交付服务、数据库状态接缝、旧发送 seam 和测试。
- 不实现 Task 8 UI、不修改 Task 9 上架/补发流程。
- 没有配置新交付方式的旧订单继续使用现有规则链路。

## Architecture

`DeliveryOrchestrationService` 接收明确的 `user_id`、`card_id`、`account_id`、`order_id`、`order_line_id`、数量和交付配置。它先安全归一化数量与订单行，再用 SQLite 中的唯一幂等状态记录协调一次交付。状态记录只保存作用域、reservation ID、模式、数量和错误元数据，不保存明文卡密。

卡密模式调用 Task 4 `reserve_items()` 一次性预占完整数量；不足时直接进入 `paused`，不产生部分 reservation，也不发送内容。足量后调用 Task 6 dispatcher，dispatcher 复用同一 reservation 的 commit 结果。发送失败保留已提交 reservation 和 `failed` 状态，显式恢复时重新读取同一 reservation 内容，因此不会重复扣库存。固定链接始终只产生一个内容；provider API 收到数量和同一幂等键，重试不会因为数量复制固定交付。

旧 `_auto_delivery` seam 保持原参数和返回结构，新增可选数量、订单行和幂等上下文，仅在调用方明确使用配置交付时生效；没有配置或没有 reservation 的调用继续走原逻辑。

## State and idempotency

状态为 `pending`、`paused`、`reserved`、`sending`、`sent`、`failed`。幂等键由 `user_id + account_id + order_id + normalized_order_line_id + card_id` 构成；订单行归一化顺序为 `order_line_id → item_id → default`。相同键的重复回调、重复重试或不同回调来源只返回既有状态和既有可公开结果，不再次 reserve、commit 或发送。`paused`/`failed` 只有显式恢复方法才会重新推进。

数量缺失按兼容旧链路的默认值 1 处理；布尔值、非整数文本、小数、0、负数和超过 100 的数量拒绝，并返回稳定的 `invalid_quantity` 错误码。错误结果包含可读错误类别和补充数量，但不包含卡密、provider token 或完整响应正文。

## Testing

测试先于实现，覆盖：数量 1/3/缺失/非法/0/负数/过大；卡密库存不足整单暂停且 available 不变；足量时 N 张不同卡密一次性 reserve/commit；固定链接只产生一条；provider payload 带数量与幂等键；重复回调、重复恢复和进程重启后的状态复用；发送失败可恢复且不重复扣卡；旧 `_prepare_configured_delivery` seam 仍保持原调用契约。最后运行 Task 7 定向、相关、全量 pytest、`py_compile` 和必要的 `node --check`。
