"""Pure conversion from account runtime state to the guided setup contract."""

from __future__ import annotations

from datetime import date, datetime
import time
from typing import Any, Mapping, Optional


STEP_TOTAL = 3
ACCOUNT_STEP_INDEX = 1
DELIVERY_STEP_INDEX = 2
READY_STEP_INDEX = STEP_TOTAL

_VERIFICATION_STATUSES = {
    "verification_pending_manual",
    "manual_verification_required",
}
_WAIT_STATUSES = {
    "qr_login_grace_wait",
    "password_login_backoff_wait",
}


def _timestamp(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).timestamp()
    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return None
        try:
            return float(raw_value)
        except ValueError:
            try:
                return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_remaining_seconds(deadline: Any, now: Any = None) -> int:
    """Return the current non-negative number of seconds until ``deadline``."""
    deadline_timestamp = _timestamp(deadline)
    if deadline_timestamp is None:
        return 0
    now_timestamp = _timestamp(now) if now is not None else time.time()
    if now_timestamp is None:
        now_timestamp = time.time()
    return max(0, int(deadline_timestamp - now_timestamp))


def _normalized_runtime_status(runtime_status: Optional[Mapping[str, Any]]) -> tuple[str, str]:
    runtime_status = runtime_status or {}
    fallback_status = str(runtime_status.get("status") or "").strip().lower()
    token_status = str(runtime_status.get("token_refresh_status") or fallback_status).strip().lower()
    connection_state = str(runtime_status.get("connection_state") or fallback_status).strip().lower()
    return token_status, connection_state


def get_user_action_for_runtime(runtime_status: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Map an internal runtime state to one user-facing action."""
    token_status, connection_state = _normalized_runtime_status(runtime_status)
    runtime_status = runtime_status or {}

    if token_status in _VERIFICATION_STATUSES:
        if runtime_status.get("manual_verification_open"):
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


def _retry_deadline(runtime_status: Mapping[str, Any], technical_status: str) -> Any:
    candidates = [
        "retry_at",
        "token_refresh_retry_at",
        "token_refresh_backoff_until",
        "qr_login_grace_until",
        "manual_verification_until",
    ]
    for key in candidates:
        value = runtime_status.get(key)
        if value not in (None, ""):
            return value

    if technical_status == "password_login_backoff_wait":
        return runtime_status.get("backoff_until")
    if technical_status == "qr_login_grace_wait":
        return runtime_status.get("grace_until")
    return None


def _delivery_configured(delivery_summary: Optional[Mapping[str, Any]]) -> Optional[bool]:
    if delivery_summary is None:
        return None
    if not isinstance(delivery_summary, Mapping):
        return bool(delivery_summary)
    for key in ("configured", "is_configured", "complete", "ready"):
        if key in delivery_summary:
            return bool(delivery_summary[key])
    return None


def _technical_status(runtime_status: Mapping[str, Any]) -> str:
    token_status, connection_state = _normalized_runtime_status(runtime_status)
    return token_status or connection_state or "unknown"


def _technical_detail(runtime_status: Mapping[str, Any], technical_status: str) -> str:
    # Keep this deliberately bounded: runtime errors may contain credentials or URLs.
    if technical_status == "unknown" and runtime_status.get("error"):
        return "内部状态异常，详细信息已隐藏。"
    return f"内部状态：{technical_status}"


def build_guided_status(
    runtime_status: Optional[Mapping[str, Any]],
    account_details: Optional[Mapping[str, Any]] = None,
    delivery_summary: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Convert runtime details into the stable guided setup status object."""
    del account_details  # Account details are intentionally never copied to the response.
    runtime_status = runtime_status or {}
    action = get_user_action_for_runtime(runtime_status)
    technical_status = _technical_status(runtime_status)
    retry_at = _retry_deadline(runtime_status, technical_status)
    status = {
        "step_id": "account_connection",
        "step_index": ACCOUNT_STEP_INDEX,
        "step_total": STEP_TOTAL,
        "title": action["title"],
        "message": action["message"],
        "needs_user_action": action["needs_user_action"],
        "primary_action": action["action"],
        "retry_at": retry_at,
        "remaining_seconds": format_remaining_seconds(retry_at),
        "technical_status": technical_status,
        "technical_detail": _technical_detail(runtime_status, technical_status),
    }

    _, connection_state = _normalized_runtime_status(runtime_status)
    if connection_state == "connected":
        configured = _delivery_configured(delivery_summary)
        if configured is True:
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

    return status


__all__ = [
    "build_guided_status",
    "format_remaining_seconds",
    "get_user_action_for_runtime",
]
