"""Quantity-aware, idempotent delivery orchestration."""

import inspect
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from card_inventory_service import CardInventoryError
from delivery_adapter_service import DeliveryDispatchError, DeliveryRequest


DELIVERY_STATES = {"pending", "paused", "reserved", "sending", "sent", "failed"}
CARD_MODES = {"imported_card", "generated_card"}
MAX_DELIVERY_QUANTITY = 100
_UNSET = object()


class DeliveryOrchestrationError(ValueError):
    def __init__(self, code: str, message: str, technical_category: str = "validation", **details):
        super().__init__(message)
        self.code = code
        self.technical_category = technical_category
        self.details = details


@dataclass(frozen=True)
class DeliveryOrchestrationRequest:
    user_id: int
    card_id: int
    account_id: str
    order_id: str
    order_line_id: str | None
    quantity: Any
    delivery_config: Mapping[str, Any]
    item_id: str | None = None
    context: Mapping[str, Any] = None
    idempotency_key: str | None = None


def normalize_quantity(value: Any, *, maximum: int = MAX_DELIVERY_QUANTITY) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return 1
    if isinstance(value, bool):
        raise DeliveryOrchestrationError("invalid_quantity", "购买数量必须是正整数")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        normalized = int(value.strip())
    else:
        raise DeliveryOrchestrationError("invalid_quantity", "购买数量必须是正整数")
    if normalized <= 0:
        raise DeliveryOrchestrationError("invalid_quantity", "购买数量必须大于 0")
    if normalized > maximum:
        raise DeliveryOrchestrationError(
            "invalid_quantity",
            f"购买数量不能超过 {maximum}",
            maximum=maximum,
        )
    return normalized


def normalize_order_line_id(order_line_id: Any, item_id: Any = None) -> str:
    for candidate in (order_line_id, item_id, "default"):
        value = str(candidate or "").strip()
        if value:
            return value
    return "default"


class DeliveryOrchestrationService:
    def __init__(self, db_manager, inventory_service, dispatcher):
        self.db = db_manager
        self.inventory = inventory_service
        self.dispatcher = dispatcher

    @staticmethod
    def _scope(request: DeliveryOrchestrationRequest):
        try:
            user_id = int(request.user_id)
            card_id = int(request.card_id)
        except (TypeError, ValueError) as exc:
            raise DeliveryOrchestrationError("invalid_scope", "用户或商品标识无效") from exc
        account_id = str(request.account_id or "").strip()
        order_id = str(request.order_id or "").strip()
        if user_id <= 0 or card_id <= 0 or not account_id or not order_id:
            raise DeliveryOrchestrationError("invalid_scope", "用户、商品、账号和订单号不能为空")
        return user_id, card_id, account_id, order_id

    @staticmethod
    def _config(config: Mapping[str, Any]):
        if not isinstance(config, Mapping):
            raise DeliveryOrchestrationError("invalid_config", "交付配置格式无效", "configuration")
        mode = str(config.get("mode") or "").strip()
        payload = config.get("config") if isinstance(config.get("config"), Mapping) else config
        payload = dict(payload)
        payload.pop("mode", None)
        if mode not in {"fixed_link", "imported_card", "generated_card", "provider_api"}:
            raise DeliveryOrchestrationError("invalid_mode", "交付方式无效")
        return mode, payload

    def _normalize_request(self, request: DeliveryOrchestrationRequest) -> DeliveryOrchestrationRequest:
        if not isinstance(request, DeliveryOrchestrationRequest):
            raise DeliveryOrchestrationError("invalid_request", "交付编排请求格式无效")
        user_id, card_id, account_id, order_id = self._scope(request)
        quantity = normalize_quantity(request.quantity)
        order_line_id = normalize_order_line_id(request.order_line_id, request.item_id)
        mode, config = self._config(request.delivery_config)
        idempotency_key = str(request.idempotency_key or "").strip()
        if not idempotency_key:
            idempotency_key = "|".join(
                (str(user_id), account_id, order_id, order_line_id, str(card_id))
            )
        context = dict(request.context or {})
        context.update(
            {
                "order_id": order_id,
                "item_id": request.item_id,
                "order_line_id": order_line_id,
                "quantity": quantity,
                "idempotency_key": idempotency_key,
                "account_id": account_id,
                "card_id": card_id,
            }
        )
        return replace(
            request,
            user_id=user_id,
            card_id=card_id,
            account_id=account_id,
            order_id=order_id,
            order_line_id=order_line_id,
            quantity=quantity,
            delivery_config={"mode": mode, **config},
            context=context,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _row_to_state(row):
        if not row:
            return None
        columns = (
            "id", "user_id", "card_id", "account_id", "order_id", "order_line_id",
            "quantity", "mode", "idempotency_key", "reservation_id", "status",
            "result_meta", "last_error_code", "last_error", "created_at",
            "updated_at", "sent_at",
        )
        state = dict(zip(columns, row))
        try:
            state["result_meta"] = json.loads(state.get("result_meta") or "{}")
        except (TypeError, ValueError):
            state["result_meta"] = {}
        return state

    def _get_state(self, request: DeliveryOrchestrationRequest):
        with self.db.lock:
            row = self.db.conn.execute(
                """
                SELECT id, user_id, card_id, account_id, order_id, order_line_id,
                       quantity, mode, idempotency_key, reservation_id, status,
                       result_meta, last_error_code, last_error, created_at,
                       updated_at, sent_at
                FROM delivery_orchestration_states
                WHERE user_id = ? AND card_id = ? AND account_id = ?
                  AND order_id = ? AND order_line_id = ?
                """,
                (
                    request.user_id,
                    request.card_id,
                    request.account_id,
                    request.order_id,
                    request.order_line_id,
                ),
            ).fetchone()
        return self._row_to_state(row)

    def _insert_state(self, request: DeliveryOrchestrationRequest, mode: str):
        with self.db.lock:
            cursor = self.db.conn.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO delivery_orchestration_states(
                        user_id, card_id, account_id, order_id, order_line_id,
                        quantity, mode, idempotency_key, status, result_meta,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', '{}',
                              CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        request.user_id,
                        request.card_id,
                        request.account_id,
                        request.order_id,
                        request.order_line_id,
                        request.quantity,
                        mode,
                        request.idempotency_key,
                    ),
                )
                row = cursor.execute(
                    """
                    SELECT id, user_id, card_id, account_id, order_id, order_line_id,
                           quantity, mode, idempotency_key, reservation_id, status,
                           result_meta, last_error_code, last_error, created_at,
                           updated_at, sent_at
                    FROM delivery_orchestration_states
                    WHERE user_id = ? AND card_id = ? AND account_id = ?
                      AND order_id = ? AND order_line_id = ?
                    """,
                    (
                        request.user_id,
                        request.card_id,
                        request.account_id,
                        request.order_id,
                        request.order_line_id,
                    ),
                ).fetchone()
                self.db.conn.commit()
            except Exception:
                self.db.conn.rollback()
                raise
        return self._row_to_state(row)

    def _update_state(self, state_id: int, *, status: str, reservation_id=_UNSET,
                      result_meta=None, error_code=None, error=None):
        if status not in DELIVERY_STATES:
            raise DeliveryOrchestrationError("invalid_state_transition", "交付状态无效")
        meta_json = json.dumps(result_meta or {}, ensure_ascii=False, separators=(",", ":"))
        reservation_sql = "reservation_id = reservation_id"
        reservation_params = ()
        if reservation_id is not _UNSET:
            reservation_sql = "reservation_id = ?"
            reservation_params = (reservation_id,)
        with self.db.lock:
            self.db.conn.execute(
                f"""
                UPDATE delivery_orchestration_states
                SET status = ?, {reservation_sql},
                    result_meta = ?, last_error_code = ?, last_error = ?,
                    sent_at = CASE WHEN ? = 'sent' THEN CURRENT_TIMESTAMP ELSE sent_at END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    status,
                    *reservation_params,
                    meta_json,
                    error_code,
                    str(error or "")[:1000] or None,
                    status,
                    state_id,
                ),
            )
            self.db.conn.commit()

    @staticmethod
    def _error_info(exc, default_category="delivery"):
        if isinstance(exc, (CardInventoryError, DeliveryDispatchError, DeliveryOrchestrationError)):
            return exc.code, str(exc), getattr(exc, "technical_category", default_category), getattr(exc, "details", {})
        return "delivery_failed", "交付处理失败，请稍后重试", default_category, {}

    @staticmethod
    def _contents(prepared, mode: str, quantity: int):
        if not isinstance(prepared, Mapping):
            raise DeliveryOrchestrationError("content_unavailable", "交付内容不可用", "delivery")
        raw = prepared.get("contents")
        if isinstance(raw, list):
            contents = [str(value) for value in raw if str(value).strip()]
        else:
            content = prepared.get("content")
            if content is None or not str(content).strip():
                raise DeliveryOrchestrationError("content_unavailable", "交付内容不可用", "delivery")
            if mode in CARD_MODES:
                contents = [line.strip() for line in str(content).splitlines() if line.strip()]
            else:
                contents = [str(content)]
        if mode in CARD_MODES and len(contents) != quantity:
            raise DeliveryOrchestrationError(
                "content_quantity_mismatch",
                "卡密内容数量与购买数量不一致",
                "delivery",
                requested=quantity,
                returned=len(contents),
            )
        if mode not in CARD_MODES:
            contents = contents[:1]
        return contents

    def _result(self, state, *, contents=None, request=None):
        result = {
            "status": state["status"],
            "user_id": state["user_id"],
            "card_id": state["card_id"],
            "account_id": state["account_id"],
            "order_id": state["order_id"],
            "order_line_id": state["order_line_id"],
            "quantity": state["quantity"],
            "mode": state["mode"],
            "idempotency_key": state["idempotency_key"],
            "reservation_id": state.get("reservation_id"),
            "error_code": state.get("last_error_code"),
            "error": state.get("last_error"),
            "meta": state.get("result_meta") or {},
        }
        if contents is not None:
            result["contents"] = list(contents)
        return result

    def _prepare(self, request: DeliveryOrchestrationRequest, *, allow_retry=False):
        request = self._normalize_request(request)
        state = self._get_state(request)
        if state and not allow_retry:
            if state["quantity"] != request.quantity or state["mode"] != request.delivery_config["mode"]:
                raise DeliveryOrchestrationError("idempotency_conflict", "同一幂等键的订单参数不一致")
            return self._result(state)
        if state is None:
            state = self._insert_state(request, request.delivery_config["mode"])
        mode = request.delivery_config["mode"]
        reservation_id = state.get("reservation_id")
        if mode in CARD_MODES and not reservation_id:
            try:
                reservation = self.inventory.reserve_items(
                    request.card_id,
                    request.user_id,
                    request.account_id,
                    request.order_id,
                    request.quantity,
                    idempotency_key=request.idempotency_key,
                )
                reservation_id = reservation["reservation_id"]
                self._update_state(state["id"], status="reserved", reservation_id=reservation_id)
                state = self._get_state(request)
            except CardInventoryError as exc:
                code, message, _, details = self._error_info(exc, "inventory")
                self._update_state(
                    state["id"], status="paused", error_code=code, error=message,
                    result_meta={"available": details.get("available"), "requested": request.quantity},
                )
                return self._result(self._get_state(request))

        delivery_request = DeliveryRequest(
            user_id=request.user_id,
            card_id=request.card_id,
            account_id=request.account_id,
            order_id=request.order_id,
            reservation_id=reservation_id,
            context=request.context,
            mode=mode,
            quantity=request.quantity,
            idempotency_key=request.idempotency_key,
            order_line_id=request.order_line_id,
            item_id=request.item_id,
            delivery_config=request.delivery_config,
        )
        try:
            prepared = self.dispatcher.prepare(delivery_request)
            contents = self._contents(prepared, mode, request.quantity)
        except Exception as exc:
            code, message, category, details = self._error_info(exc)
            self._update_state(
                state["id"], status="failed", reservation_id=reservation_id,
                error_code=code, error=message,
                result_meta={"technical_category": category, **details},
            )
            return self._result(self._get_state(request))

        self._update_state(
            state["id"], status="sending", reservation_id=reservation_id,
            result_meta={"content_count": len(contents), "technical_category": "delivery"},
            error_code=None, error=None,
        )
        return self._result(self._get_state(request), contents=contents, request=request)

    def prepare(self, request: DeliveryOrchestrationRequest):
        return self._prepare(request)

    def _sender_result(self, sender: Callable, contents, request):
        result = sender(contents, request)
        if inspect.isawaitable(result):
            raise DeliveryOrchestrationError("async_sender_required", "异步发送器请使用异步编排入口")
        if result is False:
            raise DeliveryOrchestrationError("send_failed", "交付消息发送失败", "sender")
        return result

    def orchestrate(self, request: DeliveryOrchestrationRequest, sender: Callable):
        normalized = self._normalize_request(request)
        prepared = self._prepare(normalized)
        if prepared["status"] != "sending":
            return prepared
        try:
            self._sender_result(sender, prepared.get("contents") or [], normalized)
        except Exception as exc:
            code, message, category, details = self._error_info(exc, "sender")
            state = self._get_state(normalized)
            self._update_state(
                state["id"], status="failed", error_code=code, error=message,
                result_meta={"technical_category": category, **details},
            )
            return self._result(self._get_state(normalized))
        state = self._get_state(normalized)
        self._update_state(state["id"], status="sent", error_code=None, error=None)
        return self._result(self._get_state(normalized))

    def retry(self, request: DeliveryOrchestrationRequest, sender: Callable):
        normalized = self._normalize_request(request)
        state = self._get_state(normalized)
        if not state:
            return self.orchestrate(normalized, sender)
        if state["status"] == "sent":
            return self._result(state)
        if state["status"] not in {"paused", "failed"}:
            return self._result(state)
        self._update_state(
            state["id"], status="pending", error_code=None, error=None,
            result_meta=state.get("result_meta") or {},
        )
        prepared = self._prepare(normalized, allow_retry=True)
        if prepared["status"] != "sending":
            return prepared
        try:
            self._sender_result(sender, prepared.get("contents") or [], normalized)
        except Exception as exc:
            code, message, category, details = self._error_info(exc, "sender")
            state = self._get_state(normalized)
            self._update_state(
                state["id"], status="failed", error_code=code, error=message,
                result_meta={"technical_category": category, **details},
            )
            return self._result(self._get_state(normalized))
        state = self._get_state(normalized)
        self._update_state(state["id"], status="sent", error_code=None, error=None)
        return self._result(self._get_state(normalized))
