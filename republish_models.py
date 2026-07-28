"""Data models for reusable republish templates and jobs."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, NoReturn, Optional


def _reject_non_finite_json_constant(constant: str) -> NoReturn:
    raise ValueError(f"JSON constant {constant!r} is not allowed")


def strict_json_loads(raw: str) -> Any:
    return json.loads(raw, parse_constant=_reject_non_finite_json_constant)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or None")
    return value


def _optional_number(value: Any, field_name: str) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number or None")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number or None") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _boolean(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "1"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "0"}:
        return False
    raise ValueError(f"{field_name} must be a boolean")


def _images(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = strict_json_loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("images must be a JSON array or list") from exc
    if not isinstance(value, (list, tuple)):
        raise ValueError("images must be a list")
    result = []
    for image in value:
        result.append(_required_text(image, "image"))
    return result


def _json_value(value: Any, field_name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return strict_json_loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} must contain valid JSON") from exc
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON serializable") from exc
    return value


@dataclass
class RepublishTemplate:
    template_id: str
    cookie_id: str
    current_item_id: str
    title: str = ""
    description: str = ""
    images: list[str] = field(default_factory=list)
    current_price: Optional[float] = None
    original_price: Optional[float] = None
    delivery_choice: Optional[str] = None
    post_price: Optional[float] = None
    can_self_pickup: bool = False
    category_hint: Optional[str] = None
    delivery_content: Optional[str] = field(default=None, repr=False)
    sku_delivery: Any = field(default_factory=dict, repr=False)
    auto_delivery: bool = False
    auto_republish: bool = False
    paused: bool = False

    def __repr__(self) -> str:
        return (
            "RepublishTemplate("
            f"template_id={self.template_id!r}, "
            f"cookie_id={self.cookie_id!r}, "
            f"current_item_id={self.current_item_id!r}, "
            f"auto_delivery={self.auto_delivery!r}, "
            f"auto_republish={self.auto_republish!r}, "
            f"paused={self.paused!r})"
        )

    def __post_init__(self) -> None:
        self.template_id = _required_text(self.template_id, "template_id")
        self.cookie_id = _required_text(self.cookie_id, "cookie_id")
        self.current_item_id = _required_text(self.current_item_id, "current_item_id")
        if not isinstance(self.title, str) or not isinstance(self.description, str):
            raise ValueError("title and description must be strings")
        self.images = _images(self.images)
        self.current_price = _optional_number(self.current_price, "current_price")
        self.original_price = _optional_number(self.original_price, "original_price")
        self.post_price = _optional_number(self.post_price, "post_price")
        self.delivery_choice = _optional_text(self.delivery_choice, "delivery_choice")
        self.category_hint = _optional_text(self.category_hint, "category_hint")
        self.delivery_content = _optional_text(self.delivery_content, "delivery_content")
        self.sku_delivery = _json_value(self.sku_delivery, "sku_delivery")
        self.can_self_pickup = _boolean(self.can_self_pickup, "can_self_pickup")
        self.auto_delivery = _boolean(self.auto_delivery, "auto_delivery")
        self.auto_republish = _boolean(self.auto_republish, "auto_republish")
        self.paused = _boolean(self.paused, "paused")

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "cookie_id": self.cookie_id,
            "current_item_id": self.current_item_id,
            "title": self.title,
            "description": self.description,
            "images": list(self.images),
            "current_price": self.current_price,
            "original_price": self.original_price,
            "delivery_choice": self.delivery_choice,
            "post_price": self.post_price,
            "can_self_pickup": self.can_self_pickup,
            "category_hint": self.category_hint,
            "delivery_content": self.delivery_content,
            "sku_delivery": self.sku_delivery,
            "auto_delivery": self.auto_delivery,
            "auto_republish": self.auto_republish,
            "paused": self.paused,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RepublishTemplate":
        if not isinstance(payload, Mapping):
            raise ValueError("template payload must be an object")
        return cls(
            template_id=payload.get("template_id"),
            cookie_id=payload.get("cookie_id"),
            current_item_id=payload.get("current_item_id"),
            title=payload.get("title", ""),
            description=payload.get("description", ""),
            images=payload.get("images", []),
            current_price=payload.get("current_price"),
            original_price=payload.get("original_price"),
            delivery_choice=payload.get("delivery_choice"),
            post_price=payload.get("post_price"),
            can_self_pickup=payload.get("can_self_pickup", False),
            category_hint=payload.get("category_hint"),
            delivery_content=payload.get("delivery_content"),
            sku_delivery=payload.get("sku_delivery", {}),
            auto_delivery=payload.get("auto_delivery", False),
            auto_republish=payload.get("auto_republish", False),
            paused=payload.get("paused", False),
        )

    @classmethod
    def from_json(cls, raw: str) -> "RepublishTemplate":
        try:
            payload = strict_json_loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("template_json must contain a JSON object") from exc
        return cls.from_dict(payload)


_JOB_STATUSES = {"pending", "running", "retry", "succeeded", "manual_required"}


def _timestamp(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a timestamp")
    try:
        timestamp = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a timestamp") from exc
    if not math.isfinite(timestamp):
        raise ValueError(f"{field_name} must be finite")
    return timestamp


@dataclass
class RepublishJob:
    job_id: str
    template_id: str
    source_item_id: str
    trigger_order_id: str
    status: str
    available_at: float
    attempts: int
    last_error: Optional[str] = field(repr=False)
    old_item_id: str
    new_item_id: Optional[str]
    created_at: float
    updated_at: float
    order_context: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.job_id = _required_text(self.job_id, "job_id")
        self.template_id = _required_text(self.template_id, "template_id")
        self.source_item_id = _required_text(self.source_item_id, "source_item_id")
        self.trigger_order_id = _required_text(self.trigger_order_id, "trigger_order_id")
        if self.status not in _JOB_STATUSES:
            raise ValueError(f"unsupported job status: {self.status}")
        self.available_at = _timestamp(self.available_at, "available_at")
        self.created_at = _timestamp(self.created_at, "created_at")
        self.updated_at = _timestamp(self.updated_at, "updated_at")
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int) or self.attempts < 0:
            raise ValueError("attempts must be a non-negative integer")
        self.last_error = _optional_text(self.last_error, "last_error")
        self.old_item_id = _required_text(self.old_item_id, "old_item_id")
        self.new_item_id = _optional_text(self.new_item_id, "new_item_id")
        self.order_context = _json_value(self.order_context, "order_context")


def valid_job_statuses() -> frozenset[str]:
    return frozenset(_JOB_STATUSES)
