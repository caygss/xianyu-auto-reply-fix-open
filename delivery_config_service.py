from __future__ import annotations

import ipaddress
import json
import re
import unicodedata
from contextlib import contextmanager
from urllib.parse import urlparse


DELIVERY_MODES = {
    "fixed_link",
    "imported_card",
    "generated_card",
    "provider_api",
}
MAX_DELIVERY_CONFIG_BYTES = 64 * 1024
MAX_FIXED_LINK_BYTES = 2048
MAX_PROVIDER_ENDPOINT_BYTES = 2048
MAX_PROVIDER_TIMEOUT_SECONDS = 30
MAX_PROVIDER_RETRIES = 3
MAX_PROVIDER_RESPONSE_BYTES = 64 * 1024


class DeliveryConfigError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DeliveryConfigService:
    def __init__(self, db_manager):
        self.db = db_manager

    @contextmanager
    def _transaction(self):
        with self.db.lock:
            cursor = self.db.conn.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                yield cursor
                self.db.conn.commit()
            except Exception:
                self.db.conn.rollback()
                raise

    @staticmethod
    def _scope(user_id, card_id, account_id):
        try:
            user_id = int(user_id)
            card_id = int(card_id)
        except (TypeError, ValueError) as exc:
            raise DeliveryConfigError("invalid_scope", "用户或商品标识无效") from exc
        account_id = str(account_id or "").strip()
        if user_id <= 0 or card_id <= 0 or not account_id:
            raise DeliveryConfigError("invalid_scope", "用户、商品和账号标识不能为空")
        return user_id, card_id, account_id

    @staticmethod
    def _validate_fixed_link(url):
        if not isinstance(url, str) or not url:
            raise DeliveryConfigError("invalid_config", "固定链接必须是有效的 HTTP 或 HTTPS 链接")
        if len(url.encode("utf-8")) > MAX_FIXED_LINK_BYTES:
            raise DeliveryConfigError("invalid_config", "固定链接长度超过限制")
        if any(
            char.isspace() or unicodedata.category(char) in {"Cc", "Cf"}
            for char in url
        ):
            raise DeliveryConfigError("invalid_config", "固定链接不能包含空白或控制字符")
        if re.search(r"%(?![0-9A-Fa-f]{2})", url):
            raise DeliveryConfigError("invalid_config", "固定链接包含非法 percent 编码")

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
        except (TypeError, ValueError) as exc:
            raise DeliveryConfigError(
                "invalid_config", "固定链接必须是有效的 HTTP 或 HTTPS 链接"
            ) from exc
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
            raise DeliveryConfigError("invalid_config", "固定链接必须是有效的 HTTP 或 HTTPS 链接")

        authority = parsed.netloc.rsplit("@", 1)[-1]
        if authority.startswith("["):
            closing_bracket = authority.find("]")
            suffix = authority[closing_bracket + 1:] if closing_bracket >= 0 else authority
            if closing_bracket < 0 or (suffix and not suffix.startswith(":")):
                raise DeliveryConfigError("invalid_config", "固定链接主机地址无效")
            port_text = suffix[1:] if suffix else None
            try:
                ipaddress.ip_address(hostname)
            except ValueError as exc:
                raise DeliveryConfigError("invalid_config", "固定链接主机地址无效") from exc
        else:
            if authority.count(":") > 1:
                raise DeliveryConfigError("invalid_config", "固定链接主机地址无效")
            port_text = authority.rsplit(":", 1)[1] if ":" in authority else None
            if ":" in hostname:
                raise DeliveryConfigError("invalid_config", "固定链接主机地址无效")
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                try:
                    ascii_hostname = hostname.encode("idna").decode("ascii")
                except UnicodeError as exc:
                    raise DeliveryConfigError("invalid_config", "固定链接主机地址无效") from exc
                labels = ascii_hostname.split(".")
                if (
                    len(ascii_hostname) > 253
                    or any(
                        not label
                        or len(label) > 63
                        or label[0] == "-"
                        or label[-1] == "-"
                        or not re.fullmatch(r"[A-Za-z0-9-]+", label)
                        for label in labels
                    )
                ):
                    raise DeliveryConfigError("invalid_config", "固定链接主机地址无效")

        if port_text is not None and (
            not port_text
            or not port_text.isascii()
            or not port_text.isdecimal()
        ):
            raise DeliveryConfigError("invalid_config", "固定链接端口必须是数字")
        if port_text is not None:
            try:
                port_number = int(port_text)
            except (TypeError, ValueError, OverflowError) as exc:
                raise DeliveryConfigError("invalid_config", "固定链接端口超出范围") from exc
            if not 0 <= port_number <= 65535:
                raise DeliveryConfigError("invalid_config", "固定链接端口超出范围")
        return url

    @staticmethod
    def _validate_provider_endpoint(endpoint):
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise DeliveryConfigError(
                "invalid_config", "Provider endpoint 必须是有效的 HTTP 或 HTTPS 链接"
            )
        if len(endpoint.encode("utf-8")) > MAX_PROVIDER_ENDPOINT_BYTES:
            raise DeliveryConfigError("invalid_config", "Provider endpoint 长度超过限制")
        try:
            parsed = urlparse(endpoint)
            if parsed.username or parsed.password:
                raise DeliveryConfigError("invalid_config", "Provider endpoint 不能包含账号信息")
        except (TypeError, ValueError) as exc:
            raise DeliveryConfigError(
                "invalid_config", "Provider endpoint 必须是有效的 HTTP 或 HTTPS 链接"
            ) from exc
        try:
            DeliveryConfigService._validate_fixed_link(endpoint)
        except DeliveryConfigError as exc:
            raise DeliveryConfigError(
                "invalid_config", "Provider endpoint 必须是有效的 HTTP 或 HTTPS 链接"
            ) from exc
        return endpoint

    @staticmethod
    def _validate_provider_config(config):
        endpoint = DeliveryConfigService._validate_provider_endpoint(config.get("endpoint"))

        token = config.get("token")
        if token is not None and (not isinstance(token, str) or not token.strip()):
            raise DeliveryConfigError("invalid_config", "Provider token 必须是非空文本")

        headers = config.get("headers", {})
        if not isinstance(headers, dict) or any(
            not isinstance(key, str) or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in headers.items()
        ):
            raise DeliveryConfigError("invalid_config", "Provider 请求头格式无效")

        field_mapping = config.get("field_mapping", {})
        if not isinstance(field_mapping, dict) or any(
            not isinstance(key, str) or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in field_mapping.items()
        ):
            raise DeliveryConfigError("invalid_config", "Provider 字段映射格式无效")

        response_field = config.get("response_field", "content")
        if not isinstance(response_field, str) or not response_field.strip():
            raise DeliveryConfigError("invalid_config", "Provider 响应字段不能为空")

        timeout = config.get("timeout_seconds", 10)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise DeliveryConfigError("invalid_config", "Provider 超时必须是 1 到 30 秒")
        if not 1 <= timeout <= MAX_PROVIDER_TIMEOUT_SECONDS:
            raise DeliveryConfigError("invalid_config", "Provider 超时必须是 1 到 30 秒")

        retries = config.get("max_retries", 0)
        if isinstance(retries, bool) or not isinstance(retries, int):
            raise DeliveryConfigError("invalid_config", "Provider 重试次数必须是 0 到 3 次")
        if not 0 <= retries <= MAX_PROVIDER_RETRIES:
            raise DeliveryConfigError("invalid_config", "Provider 重试次数必须是 0 到 3 次")

        request_body = config.get("request_body", {})
        if not isinstance(request_body, dict):
            raise DeliveryConfigError("invalid_config", "Provider 请求体模板必须是对象")
        return endpoint

    @staticmethod
    def _validate_config(mode, config):
        mode = str(mode or "").strip()
        if mode not in DELIVERY_MODES:
            raise DeliveryConfigError("invalid_mode", "交付方式无效")
        if not isinstance(config, dict) or not config:
            raise DeliveryConfigError("invalid_config", "交付配置不能为空")
        if any(value is None or (isinstance(value, str) and not value.strip()) for value in config.values()):
            raise DeliveryConfigError("invalid_config", "交付配置不能包含空值")
        try:
            normalized = json.loads(json.dumps(config, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise DeliveryConfigError("invalid_config", "交付配置格式无效") from exc
        if len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_DELIVERY_CONFIG_BYTES:
            raise DeliveryConfigError("invalid_config", "交付配置大小超过限制")

        if mode == "fixed_link":
            normalized["url"] = DeliveryConfigService._validate_fixed_link(
                normalized.get("url")
            )
        elif mode == "provider_api":
            DeliveryConfigService._validate_provider_config(normalized)
        return mode, normalized

    @staticmethod
    def _summary(mode, config):
        summary = {"configured": True, "field_count": len(config)}
        if mode == "fixed_link":
            try:
                summary["url_scheme"] = urlparse(config["url"]).scheme
            except (KeyError, TypeError, ValueError) as exc:
                raise DeliveryConfigError(
                    "invalid_config", "固定链接必须是有效的 HTTP 或 HTTPS 链接"
                ) from exc
        elif mode == "provider_api":
            try:
                summary["endpoint_scheme"] = urlparse(config["endpoint"]).scheme
                summary["timeout_seconds"] = config.get("timeout_seconds", 10)
                summary["max_retries"] = config.get("max_retries", 0)
            except (KeyError, TypeError, ValueError) as exc:
                raise DeliveryConfigError(
                    "invalid_config", "Provider endpoint 无法读取"
                ) from exc
        return summary

    @staticmethod
    def _response(user_id, card_id, account_id, mode, config):
        return {
            "user_id": user_id,
            "card_id": card_id,
            "account_id": account_id,
            "mode": mode,
            "config_summary": DeliveryConfigService._summary(mode, config),
        }

    def save(self, user_id, card_id, account_id, mode, config):
        user_id, card_id, account_id = self._scope(user_id, card_id, account_id)
        mode, config = self._validate_config(mode, config)
        encrypted = self.db._encrypt_secret(json.dumps(config, ensure_ascii=False, separators=(",", ":")))
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO item_delivery_configs(
                    user_id, card_id, account_id, mode, config_text,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, card_id, account_id) DO UPDATE SET
                    mode = excluded.mode,
                    config_text = excluded.config_text,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, card_id, account_id, mode, encrypted),
            )
        return self._response(user_id, card_id, account_id, mode, config)

    def get(self, user_id, card_id, account_id):
        user_id, card_id, account_id = self._scope(user_id, card_id, account_id)
        with self._transaction() as cursor:
            row = cursor.execute(
                """
                SELECT mode, config_text FROM item_delivery_configs
                WHERE user_id = ? AND card_id = ? AND account_id = ?
                """,
                (user_id, card_id, account_id),
            ).fetchone()
        if not row:
            raise DeliveryConfigError("config_not_found", "交付配置不存在")
        try:
            config = json.loads(self.db._decrypt_secret(row[1]))
        except (TypeError, ValueError) as exc:
            raise DeliveryConfigError("invalid_config", "交付配置无法读取") from exc
        return self._response(user_id, card_id, account_id, row[0], config)

    def get_for_delivery(self, user_id, card_id, account_id):
        """读取交付服务内部配置；调用方不得把 config 字段返回给 UI。"""
        user_id, card_id, account_id = self._scope(user_id, card_id, account_id)
        with self._transaction() as cursor:
            row = cursor.execute(
                """
                SELECT mode, config_text FROM item_delivery_configs
                WHERE user_id = ? AND card_id = ? AND account_id = ?
                """,
                (user_id, card_id, account_id),
            ).fetchone()
        if not row:
            raise DeliveryConfigError("config_not_found", "交付配置不存在")
        try:
            config = json.loads(self.db._decrypt_secret(row[1]))
        except (TypeError, ValueError) as exc:
            raise DeliveryConfigError("invalid_config", "交付配置无法读取") from exc
        mode, config = self._validate_config(row[0], config)
        return {
            "user_id": user_id,
            "card_id": card_id,
            "account_id": account_id,
            "mode": mode,
            "config": config,
        }

    def valid_card_ids_for_delivery(self, user_id, account_id, card_ids):
        """批量验证已保存交付配置，只返回通过校验的卡券 ID。"""
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return set()
        account_id = str(account_id or "").strip()
        if user_id <= 0 or not account_id:
            return set()
        normalized_card_ids = []
        for card_id in card_ids or []:
            if isinstance(card_id, bool):
                continue
            try:
                normalized_card_id = int(card_id)
            except (TypeError, ValueError):
                continue
            if normalized_card_id > 0 and normalized_card_id not in normalized_card_ids:
                normalized_card_ids.append(normalized_card_id)
        if not normalized_card_ids:
            return set()

        placeholders = ", ".join("?" for _ in normalized_card_ids)
        try:
            with self._transaction() as cursor:
                rows = cursor.execute(
                    f"""
                    SELECT card_id, mode, config_text
                    FROM item_delivery_configs
                    WHERE user_id = ? AND account_id = ? AND card_id IN ({placeholders})
                    """,
                    (user_id, account_id, *normalized_card_ids),
                ).fetchall()
        except Exception:
            return set()

        valid_card_ids = set()
        for card_id, mode, config_text in rows:
            try:
                config = json.loads(self.db._decrypt_secret(config_text))
                self._validate_config(mode, config)
            except Exception:
                continue
            valid_card_ids.add(int(card_id))
        return valid_card_ids

    def delete(self, user_id, card_id, account_id):
        user_id, card_id, account_id = self._scope(user_id, card_id, account_id)
        with self._transaction() as cursor:
            cursor.execute(
                """
                DELETE FROM item_delivery_configs
                WHERE user_id = ? AND card_id = ? AND account_id = ?
                """,
                (user_id, card_id, account_id),
            )
            if cursor.rowcount != 1:
                raise DeliveryConfigError("config_not_found", "交付配置不存在")
        return {"deleted": True, "card_id": card_id, "account_id": account_id}

    def count_for_account(self, user_id, account_id):
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return 0
        account_id = str(account_id or "").strip()
        if user_id <= 0 or not account_id:
            return 0
        with self._transaction() as cursor:
            return cursor.execute(
                """
                SELECT COUNT(*) FROM item_delivery_configs
                WHERE user_id = ? AND account_id = ?
                """,
                (user_id, account_id),
            ).fetchone()[0]
