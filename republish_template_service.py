"""Service helpers for building and safely resolving republish templates.

This module deliberately treats delivery content as opaque text.  It never
fetches, prints, or logs delivery links.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
from dataclasses import replace
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from republish_models import RepublishTemplate
from republish_store import RepublishStore


_SKU_FIELDS = ("sku_id", "skuId", "sku", "spec_id", "specId", "sku_name", "spec_name")
_IMAGE_FIELDS = ("images", "item_images", "pictures", "picUrls", "imageList")
_DETAIL_NESTED_FIELDS = ("data", "item", "itemInfo", "item_info")
_SUMMARY_KEY = secrets.token_bytes(32)


def normalize_delivery_content(value: Any) -> str:
    """Validate and trim an opaque delivery instruction."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("delivery_content must be a non-empty string")
    return value.strip()


def _stable_string(value: Any, field_name: str) -> Optional[str]:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{field_name} must be finite")
        return str(value)
    return None


def _normalize_scope_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} must not contain control characters")
    return normalized


def _validated_sku_delivery(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("sku_delivery must be an object")

    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _stable_string(raw_key, "sku_delivery key")
        if not key:
            raise ValueError("sku_delivery keys must be non-empty stable strings")
        try:
            result[key] = normalize_delivery_content(raw_value)
        except ValueError as exc:
            raise ValueError("sku_delivery values must be non-empty strings") from exc
    return result


def _order_sku_values(order: Any) -> list[str]:
    if not isinstance(order, Mapping):
        return []

    values: list[str] = []
    mappings: list[Mapping[str, Any]] = []
    pending = [order]
    visited: set[int] = set()
    nested_fields = (
        "sku_info",
        "skuInfo",
        "sku_data",
        "skuData",
        "sku",
        "spec",
        "item",
    )
    while pending:
        mapping = pending.pop(0)
        marker = id(mapping)
        if marker in visited:
            continue
        visited.add(marker)
        mappings.append(mapping)
        for nested_key in nested_fields:
            nested = mapping.get(nested_key)
            if isinstance(nested, Mapping):
                pending.append(nested)

    for mapping in mappings:
        for field_name in _SKU_FIELDS:
            value = _stable_string(mapping.get(field_name), field_name)
            if value and value not in values:
                values.append(value)
    return values


def resolve_delivery_content(template: RepublishTemplate, order: Any) -> str:
    """Resolve a template's SKU-specific delivery content without cross-reading."""

    if not isinstance(template, RepublishTemplate):
        raise TypeError("template must be a RepublishTemplate")

    default_content = None
    if template.delivery_content is not None:
        default_content = normalize_delivery_content(template.delivery_content)
    sku_delivery = _validated_sku_delivery(template.sku_delivery)

    for sku in _order_sku_values(order):
        if sku in sku_delivery:
            return sku_delivery[sku]
    if default_content is not None:
        return default_content
    raise ValueError("template has no usable delivery content")


def _parse_detail(item_info: Mapping[str, Any]) -> Mapping[str, Any]:
    parsed = item_info.get("item_detail_parsed")
    raw = item_info.get("item_detail")
    value = parsed if parsed is not None else raw
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value, parse_constant=_reject_json_constant)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("item_detail must contain valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("item_detail must be an object")
    return value


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"JSON constant {constant!r} is not allowed")


def _detail_sources(item_info: Mapping[str, Any], detail: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    sources = [item_info, detail]
    for source in (detail, item_info):
        for field_name in _DETAIL_NESTED_FIELDS:
            nested = source.get(field_name)
            if isinstance(nested, Mapping) and nested not in sources:
                sources.append(nested)
    return sources


def _first_value(
    sources: list[Mapping[str, Any]], field_names: tuple[str, ...], *, skip_empty: bool = False
) -> Any:
    for source in sources:
        for field_name in field_names:
            if field_name not in source:
                continue
            value = source[field_name]
            if value is None or (skip_empty and isinstance(value, str) and not value.strip()):
                continue
            return value
    return None


def _required_field(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _validate_image_reference(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("image reference must be a non-empty URL")
    reference = value.strip()
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in reference):
        raise ValueError("image reference must not contain whitespace or control characters")

    parsed = urlparse(reference)
    if parsed.scheme in {"http", "https"}:
        try:
            hostname = parsed.hostname
        except ValueError as exc:
            raise ValueError("image URL must contain a valid hostname") from exc
        if not hostname:
            raise ValueError("image URL must contain a hostname")
        return reference
    if parsed.scheme == "data":
        header, separator, payload = reference.partition(",")
        media_type = header[5:].split(";", 1)[0].lower()
        if not separator or not media_type.startswith("image/") or not payload:
            raise ValueError("data image URL must contain a non-empty image/* payload")
        return reference
    raise ValueError("image reference must use http, https, or an image data URL")


def _image_values(value: Any) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith(("[", "{")):
            try:
                value = json.loads(stripped, parse_constant=_reject_json_constant)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("images must contain valid JSON") from exc
        else:
            value = [stripped]
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError("images must be a list")

    result: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            image = _validate_image_reference(entry)
        elif isinstance(entry, Mapping):
            image_keys = tuple(
                field_name
                for field_name in ("url", "image_url", "src", "imageUrl")
                if field_name in entry
            )
            if not image_keys:
                raise ValueError("image object must contain url, image_url, src, or imageUrl")
            image = _validate_image_reference(entry[image_keys[0]])
        else:
            raise ValueError("image reference must be a URL object or string")
        result.append(image)
    return result


def _extract_images(sources: list[Mapping[str, Any]]) -> list[str]:
    for source in sources:
        for field_name in _IMAGE_FIELDS:
            if field_name in source:
                images = _image_values(source[field_name])
                if images:
                    return images
    raise ValueError("item must contain at least one image")


def _template_id(cookie_id: Any, item_id: Any) -> str:
    normalized_cookie_id = _normalize_scope_id(cookie_id, "cookie_id")
    normalized_item_id = _normalize_scope_id(item_id, "current_item_id")
    encoded = json.dumps(
        [normalized_cookie_id, normalized_item_id], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    raw = len(encoded).to_bytes(8, "big") + encoded
    return f"template-{hashlib.sha256(raw).hexdigest()[:24]}"


def build_template_from_item_info(
    cookie_id: str,
    item_id: str,
    item_info: Mapping[str, Any],
    delivery_content: Any,
    sku_delivery: Optional[Mapping[Any, Any]] = None,
    auto_delivery: bool = True,
    auto_republish: bool = True,
    paused: bool = False,
    template_id: Optional[str] = None,
) -> RepublishTemplate:
    """Build a validated reusable template from a stored item record."""

    if not isinstance(item_info, Mapping):
        raise ValueError("item_info must be an object")
    normalized_cookie_id = _normalize_scope_id(cookie_id, "cookie_id")
    normalized_item_id = _normalize_scope_id(item_id, "current_item_id")
    detail = _parse_detail(item_info)
    sources = _detail_sources(item_info, detail)
    title = _required_field(_first_value(sources, ("item_title", "title")), "title")
    description = _required_field(
        _first_value(sources, ("item_description", "description")), "description"
    )
    images = _extract_images(sources)
    normalized_delivery = normalize_delivery_content(delivery_content)
    normalized_sku_delivery = _validated_sku_delivery(sku_delivery)

    current_price = _first_value(sources, ("item_price", "current_price", "price"), skip_empty=True)
    original_price = _first_value(
        sources,
        ("original_price", "originalPrice", "market_price", "old_price"),
        skip_empty=True,
    )
    delivery_choice = _first_value(
        sources,
        ("delivery_choice", "deliveryChoice", "delivery_type", "deliveryType", "shipping_method"),
        skip_empty=True,
    )
    post_price = _first_value(
        sources,
        ("post_price", "postPrice", "postage", "freight", "shipping_fee"),
        skip_empty=True,
    )
    can_self_pickup = _first_value(
        sources,
        ("can_self_pickup", "canSelfPickup", "self_pickup", "selfPickup"),
        skip_empty=True,
    )
    if can_self_pickup is None:
        can_self_pickup = False
    category_hint = _first_value(
        sources, ("item_category", "category", "category_hint", "categoryHint"), skip_empty=True
    )
    normalized_template_id = (
        _normalize_scope_id(template_id, "template_id")
        if template_id is not None
        else _template_id(normalized_cookie_id, normalized_item_id)
    )

    return RepublishTemplate(
        template_id=normalized_template_id,
        cookie_id=normalized_cookie_id,
        current_item_id=normalized_item_id,
        title=title,
        description=description,
        images=images,
        current_price=current_price,
        original_price=original_price,
        delivery_choice=delivery_choice,
        post_price=post_price,
        can_self_pickup=can_self_pickup,
        category_hint=category_hint,
        delivery_content=normalized_delivery,
        sku_delivery=normalized_sku_delivery,
        auto_delivery=auto_delivery,
        auto_republish=auto_republish,
        paused=paused,
    )


def _required_item_id(value: Any) -> str:
    return _normalize_scope_id(value, "new item_id")


def rotate_current_item_id(
    store: RepublishStore, template_id: str, new_item_id: str
) -> RepublishTemplate:
    """Atomically move a template to a new item ID within its cookie scope."""

    if not isinstance(store, RepublishStore):
        raise TypeError("store must be a RepublishStore")
    if not isinstance(template_id, str) or not template_id.strip():
        raise ValueError("template_id must be a non-empty string")
    new_item_id = _required_item_id(new_item_id)

    with store._connection() as connection:
        store._begin(connection)
        success = False
        try:
            row = connection.execute(
                "SELECT * FROM republish_templates WHERE template_id = ?",
                (template_id.strip(),),
            ).fetchone()
            if row is None:
                raise ValueError("unknown template_id")
            old_item_id = row["current_item_id"]
            if new_item_id == old_item_id:
                raise ValueError("new item_id must differ from current item_id")
            conflict = connection.execute(
                """
                SELECT 1 FROM republish_templates
                WHERE cookie_id = ? AND current_item_id = ? AND template_id <> ?
                LIMIT 1
                """,
                (row["cookie_id"], new_item_id, row["template_id"]),
            ).fetchone()
            if conflict is not None:
                raise ValueError("new item_id is already used by another template")

            current = store._template_from_row(row)
            updated = replace(current, current_item_id=new_item_id)
            template_json = store._template_json(updated)
            connection.execute(
                """
                UPDATE republish_templates
                SET current_item_id = ?, template_json = ?, updated_at = ?
                WHERE template_id = ? AND current_item_id = ?
                """,
                (new_item_id, template_json, store._now(), row["template_id"], old_item_id),
            )
            result_row = connection.execute(
                "SELECT * FROM republish_templates WHERE template_id = ?",
                (row["template_id"],),
            ).fetchone()
            store._finish(connection, True)
            success = True
            return store._template_from_row(result_row)
        finally:
            if not success:
                store._finish(connection, False)


def safe_delivery_summary(value: Any) -> str:
    """Return a short non-reversible summary suitable for API/log metadata."""

    if not isinstance(value, str) or not value.strip():
        return "delivery:invalid"
    digest = hmac.new(_SUMMARY_KEY, value.strip().encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return f"delivery:{digest}"
