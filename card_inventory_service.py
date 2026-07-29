import hashlib
import hmac
import secrets
import uuid
from contextlib import contextmanager

from loguru import logger


DEFAULT_GENERATOR_CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class CardInventoryError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details


class CardInventoryService:
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
        except (TypeError, ValueError):
            raise CardInventoryError("invalid_scope", "商品或用户标识无效")
        account_id = str(account_id or "").strip()
        if user_id <= 0 or card_id <= 0 or not account_id:
            raise CardInventoryError("invalid_scope", "商品、用户和账号标识不能为空")
        return user_id, card_id, account_id

    @staticmethod
    def _positive_int(value, field_name, *, allow_zero=False):
        if isinstance(value, bool) or not isinstance(value, int):
            raise CardInventoryError("invalid_settings", f"{field_name}必须是整数")
        if value < 0 or (value == 0 and not allow_zero):
            raise CardInventoryError("invalid_settings", f"{field_name}必须是正整数")
        return value

    def _settings_row(self, cursor, user_id, card_id, account_id):
        row = cursor.execute(
            """
            SELECT stock_ceiling, low_stock_threshold, auto_replenish,
                   generator_prefix, generator_length, generator_charset,
                   updated_at
            FROM card_inventory_settings
            WHERE user_id = ? AND card_id = ? AND account_id = ?
            """,
            (user_id, card_id, account_id),
        ).fetchone()
        if row:
            return row
        cursor.execute(
            """
            INSERT INTO card_inventory_settings(
                user_id, card_id, account_id, stock_ceiling,
                low_stock_threshold, auto_replenish, generator_prefix,
                generator_length, generator_charset, updated_at
            ) VALUES (?, ?, ?, 100, 20, 0, '', 16, ?, CURRENT_TIMESTAMP)
            """,
            (user_id, card_id, account_id, DEFAULT_GENERATOR_CHARSET),
        )
        return cursor.execute(
            """
            SELECT stock_ceiling, low_stock_threshold, auto_replenish,
                   generator_prefix, generator_length, generator_charset,
                   updated_at
            FROM card_inventory_settings
            WHERE user_id = ? AND card_id = ? AND account_id = ?
            """,
            (user_id, card_id, account_id),
        ).fetchone()

    @staticmethod
    def _settings_dict(row):
        return {
            "stock_ceiling": row[0],
            "low_stock_threshold": row[1],
            "auto_replenish": bool(row[2]),
            "generator_prefix": row[3],
            "generator_length": row[4],
            "generator_charset": row[5],
            "updated_at": row[6],
        }

    def _outstanding_count(self, cursor, user_id, card_id, account_id):
        return cursor.execute(
            """
            SELECT COUNT(*) FROM card_inventory_items
            WHERE user_id = ? AND card_id = ? AND account_id = ?
              AND status IN ('available', 'reserved')
            """,
            (user_id, card_id, account_id),
        ).fetchone()[0]

    def save_settings(
        self,
        card_id,
        user_id,
        account_id,
        *,
        stock_ceiling=None,
        low_stock_threshold=None,
        auto_replenish=None,
        generator_prefix=None,
        generator_length=None,
        generator_charset=None,
    ):
        user_id, card_id, account_id = self._scope(user_id, card_id, account_id)
        with self._transaction() as cursor:
            current = self._settings_dict(self._settings_row(cursor, user_id, card_id, account_id))
            values = {
                "stock_ceiling": current["stock_ceiling"] if stock_ceiling is None else stock_ceiling,
                "low_stock_threshold": (
                    current["low_stock_threshold"]
                    if low_stock_threshold is None else low_stock_threshold
                ),
                "auto_replenish": current["auto_replenish"] if auto_replenish is None else auto_replenish,
                "generator_prefix": current["generator_prefix"] if generator_prefix is None else generator_prefix,
                "generator_length": current["generator_length"] if generator_length is None else generator_length,
                "generator_charset": current["generator_charset"] if generator_charset is None else generator_charset,
            }
            ceiling = self._positive_int(values["stock_ceiling"], "库存上限")
            threshold = self._positive_int(values["low_stock_threshold"], "低库存预警线", allow_zero=True)
            if threshold > ceiling:
                if low_stock_threshold is None:
                    threshold = ceiling
                else:
                    raise CardInventoryError("invalid_settings", "低库存预警线不能高于库存上限")
            if not isinstance(values["auto_replenish"], bool):
                raise CardInventoryError("invalid_settings", "自动补充开关无效")
            prefix = str(values["generator_prefix"] or "")
            length = self._positive_int(values["generator_length"], "生成长度")
            charset = str(values["generator_charset"] or "")
            if length < 4 or length <= len(prefix):
                raise CardInventoryError("invalid_settings", "生成长度必须大于前缀且不小于4")
            if not charset:
                raise CardInventoryError("invalid_settings", "生成字符集不能为空")
            outstanding = self._outstanding_count(cursor, user_id, card_id, account_id)
            if ceiling < outstanding:
                raise CardInventoryError(
                    "inventory_ceiling_exceeded",
                    "新的库存上限低于当前未发出库存，无法保存",
                )
            cursor.execute(
                """
                UPDATE card_inventory_settings
                SET stock_ceiling = ?, low_stock_threshold = ?, auto_replenish = ?,
                    generator_prefix = ?, generator_length = ?, generator_charset = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND card_id = ? AND account_id = ?
                """,
                (
                    ceiling, threshold, int(values["auto_replenish"]), prefix, length, charset,
                    user_id, card_id, account_id,
                ),
            )
            result = self._settings_row(cursor, user_id, card_id, account_id)
        logger.info(
            "卡密库存设置已保存: user_id={} card_id={} account_id={} stock_ceiling={}",
            user_id, card_id, account_id, ceiling,
        )
        return self._settings_dict(result)

    def get_inventory_summary(self, card_id, user_id, account_id):
        user_id, card_id, account_id = self._scope(user_id, card_id, account_id)
        with self._transaction() as cursor:
            settings = self._settings_row(cursor, user_id, card_id, account_id)
            rows = cursor.execute(
                """
                SELECT status, COUNT(*) FROM card_inventory_items
                WHERE user_id = ? AND card_id = ? AND account_id = ? GROUP BY status
                """,
                (user_id, card_id, account_id),
            ).fetchall()
        counts = {"available": 0, "reserved": 0, "sent": 0, "invalidated": 0}
        counts.update({status: count for status, count in rows})
        counts["total"] = sum(counts.values())
        counts.update(
            {
                "user_id": user_id,
                "card_id": card_id,
                "account_id": account_id,
                "stock_ceiling": settings[0],
                "low_stock_threshold": settings[1],
                "auto_replenish": bool(settings[2]),
            }
        )
        return counts

    def _secret_digest(self, secret_text):
        signing_key = getattr(self.db.secret_fernet, "_signing_key", b"")
        digest_key = hashlib.sha256(signing_key + b"card-inventory-digest").digest()
        return hmac.new(
            digest_key, secret_text.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def import_items(self, card_id, user_id, account_id, secrets_text, idempotency_key=None):
        user_id, card_id, account_id = self._scope(user_id, card_id, account_id)
        if secrets_text is None:
            secrets_text = []
        if isinstance(secrets_text, str):
            secrets_text = secrets_text.splitlines()
        normalized = []
        blank = 0
        duplicates = 0
        seen = set()
        with self._transaction() as cursor:
            settings = self._settings_row(cursor, user_id, card_id, account_id)
            for value in secrets_text:
                secret_text = str(value or "").strip()
                if not secret_text:
                    blank += 1
                    continue
                digest = self._secret_digest(secret_text)
                if digest in seen:
                    duplicates += 1
                    continue
                seen.add(digest)
                normalized.append((secret_text, digest))

            existing = {
                row[0]
                for row in cursor.execute(
                    """
                    SELECT secret_digest FROM card_inventory_items
                    WHERE user_id = ? AND card_id = ? AND account_id = ?
                    """,
                    (user_id, card_id, account_id),
                ).fetchall()
            }
            new_items = [item for item in normalized if item[1] not in existing]
            duplicates += len(normalized) - len(new_items)
            outstanding = self._outstanding_count(cursor, user_id, card_id, account_id)
            available_slots = settings[0] - outstanding
            if len(new_items) > available_slots:
                raise CardInventoryError(
                    "inventory_ceiling_exceeded",
                    f"导入后将超过库存上限，最多还能导入 {max(available_slots, 0)} 张",
                    available_slots=max(available_slots, 0),
                    requested=len(new_items),
                )
            for secret_text, digest in new_items:
                cursor.execute(
                    """
                    INSERT INTO card_inventory_items(
                        user_id, card_id, account_id, secret_text, secret_digest,
                        source_type, status, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'manual', 'available', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        user_id,
                        card_id,
                        account_id,
                        self.db._encrypt_secret(secret_text),
                        digest,
                        idempotency_key,
                    ),
                )
        logger.info(
            "卡密库存导入完成: user_id={} card_id={} account_id={} inserted={} duplicates={} blank={}",
            user_id, card_id, account_id, len(new_items), duplicates, blank,
        )
        return {
            "inserted": len(new_items),
            "duplicates": duplicates,
            "blank": blank,
            "available": self.get_inventory_summary(card_id, user_id, account_id)["available"],
        }

    @staticmethod
    def _generate_value(prefix, length, charset):
        random_length = max(1, length - len(prefix))
        return prefix + "".join(secrets.choice(charset) for _ in range(random_length))

    def generate_items(self, card_id, user_id, account_id):
        user_id, card_id, account_id = self._scope(user_id, card_id, account_id)
        generated_batch = uuid.uuid4().hex
        with self._transaction() as cursor:
            settings = self._settings_row(cursor, user_id, card_id, account_id)
            outstanding = self._outstanding_count(cursor, user_id, card_id, account_id)
            quantity = max(0, settings[0] - outstanding)
            if quantity == 0:
                return {"generated": 0, "stock_ceiling": settings[0]}
            existing = {
                row[0]
                for row in cursor.execute(
                    """
                    SELECT secret_digest FROM card_inventory_items
                    WHERE user_id = ? AND card_id = ? AND account_id = ?
                    """,
                    (user_id, card_id, account_id),
                ).fetchall()
            }
            generated = 0
            attempts = 0
            max_attempts = max(100, quantity * 20)
            while generated < quantity and attempts < max_attempts:
                attempts += 1
                secret_text = self._generate_value(settings[3], settings[4], settings[5])
                digest = self._secret_digest(secret_text)
                if digest in existing:
                    continue
                cursor.execute(
                    """
                    INSERT INTO card_inventory_items(
                        user_id, card_id, account_id, secret_text, secret_digest,
                        source_type, status, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'generated', 'available', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        user_id,
                        card_id,
                        account_id,
                        self.db._encrypt_secret(secret_text),
                        digest,
                        generated_batch,
                    ),
                )
                existing.add(digest)
                generated += 1
            if generated != quantity:
                raise CardInventoryError("generation_failed", "无法生成足够的唯一卡密")
        logger.info(
            "卡密库存生成完成: user_id={} card_id={} account_id={} generated={}",
            user_id, card_id, account_id, generated,
        )
        return {"generated": generated, "stock_ceiling": settings[0]}

    def replenish_generated_inventory(self, card_id, user_id, account_id):
        return self.generate_items(card_id, user_id, account_id)

    @staticmethod
    def _mask_secret(secret_text):
        text = str(secret_text or "")
        if len(text) <= 4:
            return "*" * len(text)
        return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"

    def preview_items(self, card_id, user_id, account_id, limit=20):
        user_id, card_id, account_id = self._scope(user_id, card_id, account_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100:
            raise CardInventoryError("invalid_quantity", "预览数量必须在 1 到 100 之间")
        with self._transaction() as cursor:
            rows = cursor.execute(
                """
                SELECT secret_text FROM card_inventory_items
                WHERE user_id = ? AND card_id = ? AND account_id = ?
                  AND status = 'available'
                ORDER BY id ASC LIMIT ?
                """,
                (user_id, card_id, account_id, limit),
            ).fetchall()
        return [self._mask_secret(self.db._decrypt_secret(row[0])) for row in rows]

    def _reservation_row(self, cursor, reservation_id):
        return cursor.execute(
            """
            SELECT reservation_id, user_id, card_id, account_id, order_id,
                   quantity, status, idempotency_key, created_at, updated_at,
                   committed_at, released_at
            FROM card_inventory_reservations
            WHERE reservation_id = ?
            """,
            (reservation_id,),
        ).fetchone()

    @staticmethod
    def _reservation_dict(row):
        return {
            "reservation_id": row[0],
            "user_id": row[1],
            "card_id": row[2],
            "account_id": row[3],
            "order_id": row[4],
            "quantity": row[5],
            "status": row[6],
            "idempotency_key": row[7],
            "created_at": row[8],
            "updated_at": row[9],
            "committed_at": row[10],
            "released_at": row[11],
            "items": [],
        }

    def _reservation_result(self, cursor, row, include_items=False):
        result = self._reservation_dict(row)
        if include_items and row[6] == "committed":
            item_rows = cursor.execute(
                """
                SELECT secret_text FROM card_inventory_items
                WHERE reservation_id = ? AND status = 'sent'
                ORDER BY unit_index ASC
                """,
                (row[0],),
            ).fetchall()
            result["items"] = [self.db._decrypt_secret(item[0]) for item in item_rows]
        return result

    def _existing_reservation(self, cursor, user_id, card_id, account_id, order_id, idempotency_key):
        row = cursor.execute(
            """
            SELECT reservation_id, user_id, card_id, account_id, order_id,
                   quantity, status, idempotency_key, created_at, updated_at,
                   committed_at, released_at
            FROM card_inventory_reservations
            WHERE user_id = ? AND card_id = ? AND account_id = ? AND order_id = ?
            """,
            (user_id, card_id, account_id, order_id),
        ).fetchone()
        if row or not idempotency_key:
            return row
        return cursor.execute(
            """
            SELECT reservation_id, user_id, card_id, account_id, order_id,
                   quantity, status, idempotency_key, created_at, updated_at,
                   committed_at, released_at
            FROM card_inventory_reservations
            WHERE user_id = ? AND card_id = ? AND account_id = ?
              AND idempotency_key = ?
            """,
            (user_id, card_id, account_id, idempotency_key),
        ).fetchone()

    def reserve_items(self, card_id, user_id, account_id, order_id, quantity, idempotency_key=None):
        user_id, card_id, account_id = self._scope(user_id, card_id, account_id)
        order_id = str(order_id or "").strip()
        if not order_id:
            raise CardInventoryError("invalid_order", "订单号不能为空")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise CardInventoryError("invalid_quantity", "购买数量必须是正整数")
        idempotency_key = str(idempotency_key).strip() if idempotency_key else None
        with self._transaction() as cursor:
            existing = self._existing_reservation(
                cursor, user_id, card_id, account_id, order_id, idempotency_key
            )
            if existing:
                if existing[4] != order_id or existing[5] != quantity:
                    raise CardInventoryError("idempotency_conflict", "重复订单的参数不一致")
                result = self._reservation_result(
                    cursor, existing, include_items=existing[6] == "committed"
                )
            else:
                available = cursor.execute(
                    """
                    SELECT COUNT(*) FROM card_inventory_items
                    WHERE user_id = ? AND card_id = ? AND account_id = ?
                      AND status = 'available'
                    """,
                    (user_id, card_id, account_id),
                ).fetchone()[0]
                if available < quantity:
                    raise CardInventoryError(
                        "insufficient_inventory",
                        f"库存不足，还需要 {quantity - available} 张",
                        available=available,
                        requested=quantity,
                    )
                reservation_id = uuid.uuid4().hex
                cursor.execute(
                    """
                    INSERT INTO card_inventory_reservations(
                        reservation_id, user_id, card_id, account_id, order_id,
                        quantity, status, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        reservation_id,
                        user_id,
                        card_id,
                        account_id,
                        order_id,
                        quantity,
                        idempotency_key,
                    ),
                )
                items = cursor.execute(
                    """
                    SELECT id FROM card_inventory_items
                    WHERE user_id = ? AND card_id = ? AND account_id = ?
                      AND status = 'available'
                    ORDER BY id ASC LIMIT ?
                    """,
                    (user_id, card_id, account_id, quantity),
                ).fetchall()
                if len(items) != quantity:
                    raise CardInventoryError("invalid_state_transition", "库存预占数量发生变化")
                for index, (item_id,) in enumerate(items, start=1):
                    updated = cursor.execute(
                        """
                        UPDATE card_inventory_items
                        SET status = 'reserved', order_id = ?, reservation_id = ?,
                            unit_index = ?, reserved_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND status = 'available'
                        """,
                        (order_id, reservation_id, index, item_id),
                    )
                    if updated.rowcount != 1:
                        raise CardInventoryError("invalid_state_transition", "库存预占失败")
                result = self._reservation_result(
                    cursor, self._reservation_row(cursor, reservation_id)
                )
        logger.info(
            "卡密库存已预占: user_id={} card_id={} account_id={} order_id={} quantity={} reservation_id={}",
            user_id, card_id, account_id, order_id, quantity, result["reservation_id"],
        )
        return result

    def _check_reservation_scope(self, row, user_id, card_id, account_id):
        if row is None:
            raise CardInventoryError("reservation_not_found", "预占记录不存在")
        if row[1] != user_id or row[2] != card_id or row[3] != account_id:
            raise CardInventoryError("scope_mismatch", "预占记录不属于当前商品或账号")

    def _reservation_scope(self, user_id, card_id, account_id):
        try:
            user_id = int(user_id)
            card_id = int(card_id)
        except (TypeError, ValueError):
            raise CardInventoryError("invalid_scope", "商品或用户标识无效")
        account_id = str(account_id or "").strip()
        if user_id <= 0 or card_id <= 0 or not account_id:
            raise CardInventoryError("invalid_scope", "商品、用户和账号标识不能为空")
        return user_id, card_id, account_id

    def commit_reservation(self, reservation_id, user_id, card_id, account_id):
        user_id, card_id, account_id = self._reservation_scope(user_id, card_id, account_id)
        with self._transaction() as cursor:
            row = self._reservation_row(cursor, reservation_id)
            self._check_reservation_scope(row, user_id, card_id, account_id)
            if row[6] != "reserved":
                result = self._reservation_result(
                    cursor, row, include_items=row[6] == "committed"
                )
            else:
                items = cursor.execute(
                    """
                    SELECT id FROM card_inventory_items
                    WHERE reservation_id = ? AND status = 'reserved'
                    ORDER BY unit_index ASC
                    """,
                    (reservation_id,),
                ).fetchall()
                if len(items) != row[5]:
                    raise CardInventoryError(
                        "invalid_state_transition", "预占卡密数量与订单数量不一致"
                    )
                cursor.execute(
                    """
                    UPDATE card_inventory_items
                    SET status = 'sent', delivered_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE reservation_id = ? AND status = 'reserved'
                    """,
                    (reservation_id,),
                )
                cursor.execute(
                    """
                    UPDATE card_inventory_reservations
                    SET status = 'committed', committed_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE reservation_id = ? AND status = 'reserved'
                    """,
                    (reservation_id,),
                )
                result = self._reservation_result(
                    cursor, self._reservation_row(cursor, reservation_id), include_items=True
                )
        logger.info(
            "卡密库存预占已提交: user_id={} card_id={} account_id={} reservation_id={} quantity={}",
            user_id, card_id, account_id, reservation_id, result["quantity"],
        )
        return result

    def release_reservation(self, reservation_id, user_id, card_id, account_id):
        user_id, card_id, account_id = self._reservation_scope(user_id, card_id, account_id)
        with self._transaction() as cursor:
            row = self._reservation_row(cursor, reservation_id)
            self._check_reservation_scope(row, user_id, card_id, account_id)
            if row[6] != "reserved":
                result = self._reservation_result(cursor, row)
            else:
                cursor.execute(
                    """
                    UPDATE card_inventory_items
                    SET status = 'available', order_id = NULL, reservation_id = NULL,
                        unit_index = NULL, reserved_at = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE reservation_id = ? AND status = 'reserved'
                    """,
                    (reservation_id,),
                )
                cursor.execute(
                    """
                    UPDATE card_inventory_reservations
                    SET status = 'released', released_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE reservation_id = ? AND status = 'reserved'
                    """,
                    (reservation_id,),
                )
                result = self._reservation_result(
                    cursor, self._reservation_row(cursor, reservation_id)
                )
        logger.info(
            "卡密库存预占已释放: user_id={} card_id={} account_id={} reservation_id={} quantity={}",
            user_id, card_id, account_id, reservation_id, result["quantity"],
        )
        return result
