# Task 6 交付渠道适配器设计

## 目标

建立统一的交付 dispatcher，支持 `fixed_link`、`imported_card`、`generated_card`、`provider_api` 四种 mode。dispatcher 接收完整作用域和上游已创建的 `reservation_id`，输出可发送交付内容或渠道准备结果；不负责订单数量、幂等、整单暂停或兑换验证。

## 边界与数据流

`DeliveryDispatcher.prepare(request)` 先用完整的 `(user_id, card_id, account_id)` 读取仅供服务内部使用的交付配置，再按 mode 分派：固定链接直接输出 URL；两种卡密 mode 通过注入的 `CardInventoryService` 按 reservation 和完整作用域提交/读取卡密，合并为文本；provider mode 通过注入的同步 JSON transport 调用外部 endpoint，提取配置字段映射指定的响应内容。dispatcher 输出统一结构，供现有 `XianyuLive` 发送步骤转换为 text step。

provider 配置支持 `endpoint`、`token`、`headers`、`field_mapping`、`response_field`、`timeout_seconds`、`max_retries` 和可选 `request_body`。token 只存在服务内部，默认以 Bearer 认证加入请求；请求/响应/异常日志都不包含 token、卡密或 body。endpoint 只允许合法 `http`/`https` URL；超时、重试、响应体大小均有上限。仅对网络异常、超时和 429/5xx 做有限重试，4xx、非法 JSON、缺少响应字段直接返回领域错误并保留异常 cause。

## 错误与安全

统一抛出带 `code`、中文 `message`、`technical_category` 的 `DeliveryDispatchError`。未知 mode、缺失配置、无效作用域、reservation 作用域不匹配和 provider 校验/调用/响应错误均转换为可读领域错误；不把完整敏感配置返回给 UI。现有 `DeliveryConfigService` 继续返回摘要，并在保存时校验 provider endpoint 和边界。

## 现有链路接入

在 `XianyuLive` 增加可选的 configured-delivery preparation seam：只有上游明确传入 reservation 时才调用 dispatcher，随后复用已有 `_build_delivery_steps`/`send_delivery_steps_once`；默认旧链路行为不变。Task 7 负责在订单流程中创建 reservation、决定数量/幂等和暂停策略。

## 测试

全部 provider 测试使用 fake transport，不产生真实网络请求。覆盖四种 mode、未知/缺失配置、完整作用域、token 脱敏、URL/超时/重试/响应大小校验、有限重试、4xx/5xx/坏 JSON/缺字段和现有发送 seam。
