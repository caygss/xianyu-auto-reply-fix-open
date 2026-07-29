"""Pure conversion from account runtime state to the guided setup contract."""

from __future__ import annotations

from datetime import date, datetime
import math
import re
import time
from collections.abc import Mapping
from typing import Any, Optional


STEP_TOTAL = 6
PREPARE_STEP_INDEX = 1
QR_LOGIN_STEP_INDEX = 2
QR_STABILIZING_STEP_INDEX = 3
MANUAL_OR_BACKOFF_STEP_INDEX = 4
RECONNECT_STEP_INDEX = 5
DELIVERY_STEP_INDEX = RECONNECT_STEP_INDEX
READY_STEP_INDEX = 6

_VERIFICATION_STATUSES = {
    "verification_pending_manual",
    "manual_verification_required",
}
_WAIT_STATUSES = {
    "qr_login_grace_wait",
    "password_login_backoff_wait",
}
_QR_LOGIN_STATUSES = {
    "qr_login",
    "qr_login_wait",
    "qr_login_pending",
    "qr_login_scanning",
    "waiting_for_qr",
    "qr_waiting",
    "scanning",
    "scanned",
}
_ERROR_STATUSES = {
    "captcha_max_retries_exceeded",
    "token_expired_recovery_failed",
    "token_refresh_failed",
    "token_refresh_exception",
    "token_init_failed",
}
_MESSAGE_STREAM_UNREADY_STATUSES = {
    "connection_unready",
    "recovering",
    "suspected_stale",
    "unready",
    "not_ready",
}


def _timestamp(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        try:
            timestamp = value.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
        return timestamp if math.isfinite(timestamp) else None
    if isinstance(value, date):
        try:
            timestamp = datetime.combine(value, datetime.min.time()).timestamp()
        except (OverflowError, OSError, ValueError):
            return None
        return timestamp if math.isfinite(timestamp) else None
    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return None
        try:
            timestamp = float(raw_value)
            return timestamp if math.isfinite(timestamp) else None
        except ValueError:
            try:
                timestamp = datetime.fromisoformat(raw_value.replace("Z", "+00:00")).timestamp()
                return timestamp if math.isfinite(timestamp) else None
            except (OverflowError, OSError, ValueError):
                return None
    try:
        timestamp = float(value)
        return timestamp if math.isfinite(timestamp) else None
    except (OverflowError, TypeError, ValueError):
        return None


def format_remaining_seconds(deadline: Any, now: Any = None) -> int:
    """Return the current non-negative number of seconds until ``deadline``."""
    deadline_timestamp = _timestamp(deadline)
    if deadline_timestamp is None:
        return 0
    now_timestamp = _timestamp(now) if now is not None else time.time()
    if now_timestamp is None:
        now_timestamp = time.time()
    try:
        return max(0, math.ceil(deadline_timestamp - now_timestamp))
    except (OverflowError, ValueError):
        return 0


def _normalized_runtime_status(runtime_status: Optional[Mapping[str, Any]]) -> tuple[str, str]:
    runtime_status = runtime_status if isinstance(runtime_status, Mapping) else {}
    fallback_status = str(runtime_status.get("status") or "").strip().lower()
    token_status = str(runtime_status.get("token_refresh_status") or fallback_status).strip().lower()
    connection_state = str(runtime_status.get("connection_state") or fallback_status).strip().lower()
    return token_status, connection_state


def _future_deadline(runtime_status: Mapping[str, Any], keys: tuple[str, ...]) -> Optional[float]:
    now = time.time()
    for key in keys:
        timestamp = _timestamp(runtime_status.get(key))
        if timestamp is not None and timestamp > now:
            return timestamp
    return None


def _message_stream_unready(runtime_status: Mapping[str, Any]) -> bool:
    runtime_status = runtime_status if isinstance(runtime_status, Mapping) else {}
    _, connection_state = _normalized_runtime_status(runtime_status)
    stream_status = re.sub(
        r"[\s-]+",
        "_",
        str(runtime_status.get("message_stream_status") or "").strip().lower(),
    )
    if stream_status in _MESSAGE_STREAM_UNREADY_STATUSES:
        return True
    if "message_stream_ready" not in runtime_status:
        return connection_state == "connected"
    ready_value = runtime_status.get("message_stream_ready")
    if isinstance(ready_value, bool):
        return ready_value is False
    normalized_ready = str(ready_value or "").strip().lower()
    if normalized_ready in {"false", "0", "no", "off", "unready", "not_ready"}:
        return True
    if normalized_ready in {"true", "1", "yes", "on", "ready"}:
        return False
    return connection_state == "connected"


def get_user_action_for_runtime(runtime_status: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Map an internal runtime state to one user-facing action."""
    token_status, connection_state = _normalized_runtime_status(runtime_status)
    runtime_status = runtime_status if isinstance(runtime_status, Mapping) else {}

    if _message_stream_unready(runtime_status):
        return {
            "action": "refresh_status",
            "title": "正在恢复连接",
            "message": "连接状态尚未稳定，请保持浏览器窗口打开并等待系统自动恢复。",
            "needs_user_action": False,
        }

    manual_action = str(runtime_status.get("manual_verification_action") or "").strip().lower()
    if manual_action == "complete_pending":
        return {
            "action": "refresh_status",
            "title": "\u6b63\u5728\u68c0\u67e5\u9a8c\u8bc1\u7ed3\u679c",
            "message": "\u5df2\u8bb0\u5f55\u5b8c\u6210\u63d0\u793a\uff0c\u7cfb\u7edf\u6b63\u5728\u91cd\u65b0\u68c0\u67e5\u8d26\u53f7\uff0c\u8bf7\u7a0d\u5019\u3002",
            "needs_user_action": False,
        }
    if manual_action in {"open_pending", "opened"} or runtime_status.get("manual_verification_open"):
        return {
            "action": "complete_manual_verification",
            "title": "\u9700\u8981\u786e\u8ba4\u9a8c\u8bc1\u5df2\u5b8c\u6210",
            "message": "\u5b8c\u6210\u9875\u9762\u4e2d\u7684\u9a8c\u8bc1\u540e\uff0c\u8bf7\u786e\u8ba4\u5df2\u5b8c\u6210\uff0c\u7cfb\u7edf\u4f1a\u91cd\u65b0\u68c0\u67e5\u8d26\u53f7\u3002",
            "needs_user_action": True,
        }
    if token_status not in _VERIFICATION_STATUSES:
        if _future_deadline(runtime_status, ("qr_login_grace_until", "grace_until")) is not None:
            return {
                "action": "refresh_status",
                "title": "\u8d26\u53f7\u6b63\u5728\u7a33\u5b9a",
                "message": "\u626b\u7801\u767b\u5f55\u5df2\u5b8c\u6210\uff0c\u7cfb\u7edf\u6b63\u5728\u7a33\u5b9a\u8d26\u53f7\uff0c\u8bf7\u7a0d\u5019\u3002",
                "needs_user_action": False,
            }
        if _future_deadline(runtime_status, ("token_refresh_backoff_until", "backoff_until")) is not None:
            return {
                "action": "refresh_status",
                "title": "\u8bf7\u7a0d\u5019\u6062\u590d\u8d26\u53f7",
                "message": "\u7cfb\u7edf\u6b63\u5728\u7b49\u5f85\u4e0b\u4e00\u6b21\u6062\u590d\u673a\u4f1a\uff0c\u8bf7\u7a0d\u5019\u3002",
                "needs_user_action": False,
            }

    if token_status in _VERIFICATION_STATUSES:
        manual_action = str(runtime_status.get("manual_verification_action") or "").strip().lower()
        if manual_action == "complete_pending":
            return {
                "action": "refresh_status",
                "title": "正在检查验证结果",
                "message": "已收到完成提示，系统正在重新检查账号，请稍候。",
                "needs_user_action": False,
            }
        if manual_action in {"open_pending", "opened"} or runtime_status.get("manual_verification_open"):
            return {
                "action": "complete_manual_verification",
                "title": "需要确认验证已完成",
                "message": "完成页面中的验证后，请确认已完成，系统会重新检查账号。",
                "needs_user_action": True,
            }
        return {
            "action": "open_manual_verification",
            "title": "需要完成账号验证",
            "message": "请打开验证页面并完成页面中的验证。",
            "needs_user_action": True,
        }

    if token_status in _WAIT_STATUSES:
        if token_status == "qr_login_grace_wait":
            return {
                "action": "refresh_status",
                "title": "账号正在稳定",
                "message": "扫码登录已完成，系统正在稳定账号，当前无需操作。",
                "needs_user_action": False,
            }
        return {
            "action": "refresh_status",
            "title": "请稍候恢复账号",
            "message": "系统正在等待下一次恢复机会，请稍候。",
            "needs_user_action": False,
        }

    if token_status in _ERROR_STATUSES:
        return {
            "action": "refresh_status",
            "title": "自动验证未完成",
            "message": "自动验证暂未完成，请重新检查状态。",
            "needs_user_action": True,
        }

    if connection_state in {"connecting", "reconnecting"}:
        return {
            "action": "refresh_status",
            "title": "正在恢复账号连接",
            "message": "账号正在恢复连接，请稍候，系统会自动继续。",
            "needs_user_action": False,
        }

    if connection_state == "connected":
        return {
            "action": "finish",
            "title": "账号已连接",
            "message": "账号已连接，可以继续配置交付内容。",
            "needs_user_action": False,
        }

    return {
        "action": "refresh_status",
        "title": "正在检查账号状态",
        "message": "正在检查账号状态，请稍候。",
        "needs_user_action": False,
    }


def _retry_deadline(runtime_status: Mapping[str, Any], technical_status: str) -> Optional[float]:
    if technical_status == "qr_login_grace_wait":
        candidates = ["qr_login_grace_until", "grace_until"]
    elif technical_status == "password_login_backoff_wait":
        candidates = ["token_refresh_backoff_until", "backoff_until"]
    else:
        candidates = []
    candidates.extend([
        "qr_login_grace_until",
        "token_refresh_backoff_until",
        "retry_at",
        "token_refresh_retry_at",
        "manual_verification_until",
    ])
    timestamps = []
    for key in dict.fromkeys(candidates):
        timestamp = _timestamp(runtime_status.get(key))
        if timestamp is not None and timestamp > 0:
            timestamps.append(timestamp)
    if timestamps:
        active_timestamps = [timestamp for timestamp in timestamps if timestamp > time.time()]
        timestamp = active_timestamps[0] if active_timestamps else timestamps[0]
        return int(timestamp) if timestamp.is_integer() else timestamp
    return None


def _coerce_configured(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return math.isfinite(float(value)) and bool(value)
    normalized = str(value or "").strip().lower()
    return normalized in {"1", "true", "yes", "y", "on", "enabled"}


def _delivery_configured(delivery_summary: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(delivery_summary, Mapping):
        return False
    for key in ("configured", "is_configured", "complete", "ready"):
        if key in delivery_summary:
            return _coerce_configured(delivery_summary[key])
    return False


def _safe_status_value(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized or len(normalized) > 64 or not re.fullmatch(r"[a-z0-9_.-]+", normalized):
        return "unknown"
    return normalized


def _technical_status(runtime_status: Mapping[str, Any]) -> str:
    token_status, connection_state = _normalized_runtime_status(runtime_status)
    if _message_stream_unready(runtime_status):
        return "connection_unready"
    manual_action = str(runtime_status.get("manual_verification_action") or "").strip().lower()
    if manual_action in {"open_pending", "opened", "complete_pending"} or runtime_status.get(
        "manual_verification_open"
    ):
        return "verification_pending_manual"
    if token_status not in _VERIFICATION_STATUSES:
        if _future_deadline(runtime_status, ("qr_login_grace_until", "grace_until")) is not None:
            return "qr_login_grace_wait"
        if _future_deadline(runtime_status, ("token_refresh_backoff_until", "backoff_until")) is not None:
            return "password_login_backoff_wait"
    return _safe_status_value(token_status or connection_state)


def _technical_detail(runtime_status: Mapping[str, Any], technical_status: str) -> str:
    # Keep this deliberately bounded: runtime errors may contain credentials or URLs.
    if technical_status == "unknown" and runtime_status.get("error"):
        return "内部状态异常，详细信息已隐藏。"
    return f"内部状态：{technical_status}"


def _guided_step_index(
    runtime_status: Mapping[str, Any],
    technical_status: str,
    action: str,
) -> int:
    """Map the runtime state to the six visible guided-login steps."""
    runtime_status = runtime_status if isinstance(runtime_status, Mapping) else {}
    token_status, connection_state = _normalized_runtime_status(runtime_status)

    if _message_stream_unready(runtime_status):
        return RECONNECT_STEP_INDEX
    if action == "finish":
        return READY_STEP_INDEX
    if action == "go_to_delivery_config":
        return DELIVERY_STEP_INDEX
    if technical_status == "qr_login_grace_wait":
        return QR_STABILIZING_STEP_INDEX
    if (
        technical_status in _VERIFICATION_STATUSES
        or technical_status == "password_login_backoff_wait"
        or technical_status in _ERROR_STATUSES
    ):
        return MANUAL_OR_BACKOFF_STEP_INDEX
    if connection_state in {"connecting", "reconnecting", "disconnected", "connection_unready"}:
        return RECONNECT_STEP_INDEX
    if token_status in _QR_LOGIN_STATUSES or connection_state in _QR_LOGIN_STATUSES:
        return QR_LOGIN_STEP_INDEX
    if connection_state == "connected":
        return RECONNECT_STEP_INDEX
    return PREPARE_STEP_INDEX


def build_guided_status(
    runtime_status: Optional[Mapping[str, Any]],
    account_details: Optional[Mapping[str, Any]] = None,
    delivery_summary: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Convert runtime details into the stable guided setup status object."""
    del account_details  # Account details are intentionally never copied to the response.
    runtime_status = runtime_status if isinstance(runtime_status, Mapping) else {}
    action = get_user_action_for_runtime(runtime_status)
    technical_status = _technical_status(runtime_status)
    retry_at = _retry_deadline(runtime_status, technical_status)
    status = {
        "step_id": "account_connection",
        "step_index": _guided_step_index(runtime_status, technical_status, action["action"]),
        "step_total": STEP_TOTAL,
        "title": action["title"],
        "message": action["message"],
        "needs_user_action": action["needs_user_action"],
        "primary_action": action["action"],
        "retry_at": retry_at,
        "remaining_seconds": format_remaining_seconds(retry_at),
        "technical_status": technical_status,
        "technical_detail": _technical_detail(runtime_status, technical_status),
        "manual_browser_available": bool(
            runtime_status.get("vnc_manual_action_available") is True
            or str(runtime_status.get("manual_browser_session_status") or "").strip().lower()
            in {"processing", "active", "ready", "opened"}
        ),
    }

    _, connection_state = _normalized_runtime_status(runtime_status)
    active_deadline = retry_at is not None and format_remaining_seconds(retry_at) > 0
    blocking_runtime_state = (
        technical_status in _VERIFICATION_STATUSES
        or technical_status in _WAIT_STATUSES
        or action["action"] in {"open_manual_verification", "complete_manual_verification"}
        or _message_stream_unready(runtime_status)
        or active_deadline
    )
    if connection_state == "connected" and not blocking_runtime_state:
        configured = _delivery_configured(delivery_summary)
        if configured:
            status.update(
                {
                    "step_id": "ready_to_wait_for_order",
                    "step_index": READY_STEP_INDEX,
                    "title": "可以等待买家下单",
                    "message": "账号已连接，交付配置已完成，现在可以等待买家下单。",
                    "needs_user_action": False,
                    "primary_action": "finish",
                }
            )
        elif configured is False:
            status.update(
                {
                    "step_id": "delivery_config",
                    "step_index": DELIVERY_STEP_INDEX,
                    "title": "请配置交付内容",
                    "message": "账号已连接，请先配置交付内容。",
                    "needs_user_action": True,
                    "primary_action": "go_to_delivery_config",
                }
            )
        else:
            status["step_id"] = "account_connected"
    elif connection_state == "connected" and active_deadline and status["primary_action"] == "finish":
        status.update(
            {
                "title": "\u8bf7\u7a0d\u5019\u7cfb\u7edf\u5b8c\u6210\u6062\u590d",
                "message": "\u7cfb\u7edf\u6b63\u5728\u7b49\u5f85\u4e0b\u4e00\u6b21\u6062\u590d\u673a\u4f1a\uff0c\u8bf7\u5728\u7a33\u5b9a\u540e\u518d\u68c0\u67e5\u8d26\u53f7\u3002",
                "needs_user_action": False,
                "primary_action": "refresh_status",
            }
        )

    return status


__all__ = [
    "build_guided_status",
    "format_remaining_seconds",
    "get_user_action_for_runtime",
]
