from __future__ import annotations

import json
from contextlib import contextmanager
from urllib.parse import urlparse


DELIVERY_MODES = {
    "fixed_link",
    "imported_card",
    "generated_card",
    "provider_api",
}


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

        if mode == "fixed_link":
            url = str(normalized.get("url") or "").strip()
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise DeliveryConfigError("invalid_config", "固定链接必须是有效的 HTTP 或 HTTPS 链接")
            normalized["url"] = url
        return mode, normalized

    @staticmethod
    def _summary(mode, config):
        summary = {"configured": True, "field_count": len(config)}
        if mode == "fixed_link":
            summary["url_scheme"] = urlparse(config["url"]).scheme
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
