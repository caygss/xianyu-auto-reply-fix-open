from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from card_inventory_service import CardInventoryError
from delivery_config_service import (
    DELIVERY_MODES,
    MAX_PROVIDER_RESPONSE_BYTES,
    DeliveryConfigError,
    DeliveryConfigService,
)


@dataclass(frozen=True)
class DeliveryRequest:
    user_id: int
    card_id: int
    account_id: str
    order_id: str | None = None
    reservation_id: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)
    mode: str | None = None


@dataclass(frozen=True)
class ProviderResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes | str


class JsonTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any],
        timeout: float,
    ) -> ProviderResponse:
        ...


class DeliveryDispatchError(ValueError):
    def __init__(self, code: str, message: str, technical_category: str, **details):
        super().__init__(message)
        self.code = code
        self.technical_category = technical_category
        self.details = details


class UrllibJsonTransport:
    def request(self, method, url, headers, json_body, timeout):
        body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = UrlRequest(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return ProviderResponse(
                    int(response.status),
                    dict(response.headers.items()),
                    response.read(MAX_PROVIDER_RESPONSE_BYTES + 1),
                )
        except HTTPError as exc:
            return ProviderResponse(
                int(exc.code),
                dict(exc.headers.items()) if exc.headers else {},
                exc.read(MAX_PROVIDER_RESPONSE_BYTES + 1),
            )
        except (URLError, TimeoutError, OSError):
            raise


class FixedLinkAdapter:
    def prepare(self, config, request):
        return {
            "mode": "fixed_link",
            "content": config["url"],
            "content_type": "text",
        }


class CardInventoryAdapter:
    def __init__(self, inventory_service, mode):
        self.inventory_service = inventory_service
        self.mode = mode

    def prepare(self, config, request):
        reservation_id = str(request.reservation_id or "").strip()
        if not reservation_id:
            raise DeliveryDispatchError(
                "reservation_required", "卡密交付需要上游提供预占记录", "inventory"
            )
        try:
            reservation = self.inventory_service.commit_reservation(
                reservation_id,
                request.user_id,
                request.card_id,
                request.account_id,
            )
        except CardInventoryError as exc:
            message = str(exc)
            if exc.code == "scope_mismatch":
                message = "卡密预占记录与当前用户、商品或账号作用域不匹配"
            raise DeliveryDispatchError(exc.code, message, "inventory", **exc.details) from exc

        items = reservation.get("items") if isinstance(reservation, dict) else None
        if not isinstance(items, list) or not items or any(not str(item).strip() for item in items):
            raise DeliveryDispatchError("card_content_unavailable", "预占卡密内容不可用", "inventory")
        return {
            "mode": self.mode,
            "content": "\n".join(str(item) for item in items),
            "content_type": "text",
        }


class ProviderApiAdapter:
    def __init__(self, transport: JsonTransport):
        self.transport = transport

    @staticmethod
    def _payload(config, request):
        payload = copy.deepcopy(config.get("request_body", {}))
        context = dict(request.context or {})
        mapping = config.get("field_mapping", {})
        if not mapping:
            payload.update(context)
            return payload
        for source, target in mapping.items():
            if source in context:
                payload[target] = context[source]
            elif target in context:
                payload[source] = context[target]
            else:
                raise DeliveryDispatchError(
                    "provider_field_missing",
                    f"Provider 请求字段缺少：{source}",
                    "provider_request",
                )
        return payload

    @staticmethod
    def _response_value(value, path):
        current = value
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                raise DeliveryDispatchError(
                    "provider_response_field_missing",
                    "Provider 响应缺少交付内容字段",
                    "provider_response",
                )
            current = current[part]
        if current is None or (isinstance(current, str) and not current.strip()):
            raise DeliveryDispatchError(
                "provider_response_field_missing",
                "Provider 响应交付内容为空",
                "provider_response",
            )
        return current if isinstance(current, str) else str(current)

    def prepare(self, config, request):
        _, config = DeliveryConfigService._validate_config("provider_api", config)
        headers = {str(key): str(value) for key, value in config.get("headers", {}).items()}
        if not any(key.lower() == "content-type" for key in headers):
            headers["Content-Type"] = "application/json"
        if not any(key.lower() == "accept" for key in headers):
            headers["Accept"] = "application/json"
        if config.get("token") and not any(key.lower() == "authorization" for key in headers):
            headers["Authorization"] = f"Bearer {config['token']}"

        payload = self._payload(config, request)
        retries = config.get("max_retries", 0)
        timeout = config.get("timeout_seconds", 10)
        response = None
        for attempt in range(retries + 1):
            try:
                response = self.transport.request(
                    "POST", config["endpoint"], headers, payload, timeout
                )
            except Exception as exc:
                if attempt < retries:
                    continue
                raise DeliveryDispatchError(
                    "provider_transport_error",
                    "外部交付服务调用失败，请稍后重试",
                    "provider_transport",
                ) from exc
            if response.status_code in {429, *range(500, 600)} and attempt < retries:
                continue
            break

        if not isinstance(response, ProviderResponse):
            raise DeliveryDispatchError(
                "provider_transport_error", "外部交付服务返回格式异常", "provider_transport"
            )
        if isinstance(response.body, bytes):
            if len(response.body) > MAX_PROVIDER_RESPONSE_BYTES:
                raise DeliveryDispatchError(
                    "provider_response_too_large", "外部交付服务响应超过大小限制", "provider_response"
                )
            try:
                raw_body = response.body.decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise DeliveryDispatchError(
                    "provider_response_invalid", "外部交付服务返回的 JSON 无法解析", "provider_response"
                ) from exc
        elif isinstance(response.body, str):
            if len(response.body.encode("utf-8")) > MAX_PROVIDER_RESPONSE_BYTES:
                raise DeliveryDispatchError(
                    "provider_response_too_large", "外部交付服务响应超过大小限制", "provider_response"
                )
            raw_body = response.body
        else:
            raise DeliveryDispatchError(
                "provider_response_invalid", "外部交付服务响应格式异常", "provider_response"
            )

        if not 200 <= response.status_code < 300:
            raise DeliveryDispatchError(
                "provider_http_error",
                f"外部交付服务返回 HTTP {response.status_code}",
                "provider_http",
            )
        try:
            decoded = json.loads(raw_body)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise DeliveryDispatchError(
                "provider_response_invalid", "外部交付服务返回的 JSON 无法解析", "provider_response"
            ) from exc
        content = self._response_value(decoded, config.get("response_field", "content"))
        return {"mode": "provider_api", "content": content, "content_type": "text"}


class DeliveryDispatcher:
    def __init__(self, config_service, inventory_service, *, transport=None):
        self.config_service = config_service
        self.inventory_service = inventory_service
        self.transport = transport or UrllibJsonTransport()

    @staticmethod
    def _config_error(exc):
        category = "configuration"
        if exc.code in {"invalid_scope", "invalid_mode"}:
            category = "validation"
        return DeliveryDispatchError(exc.code, str(exc), category)

    def prepare(self, request):
        if not isinstance(request, DeliveryRequest):
            raise DeliveryDispatchError("invalid_request", "交付请求格式无效", "validation")
        if request.mode is not None and request.mode not in DELIVERY_MODES:
            raise DeliveryDispatchError("invalid_mode", "交付方式无效", "validation")
        try:
            config_record = self.config_service.get_for_delivery(
                request.user_id, request.card_id, request.account_id
            )
        except DeliveryConfigError as exc:
            raise self._config_error(exc) from exc
        mode = config_record["mode"]
        if request.mode is not None and request.mode != mode:
            raise DeliveryDispatchError("mode_mismatch", "请求交付方式与配置不一致", "configuration")
        config = config_record["config"]
        if mode == "fixed_link":
            return FixedLinkAdapter().prepare(config, request)
        if mode in {"imported_card", "generated_card"}:
            return CardInventoryAdapter(self.inventory_service, mode).prepare(config, request)
        if mode == "provider_api":
            return ProviderApiAdapter(self.transport).prepare(config, request)
        raise DeliveryDispatchError("invalid_mode", "交付方式无效", "validation")
