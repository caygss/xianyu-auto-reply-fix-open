"""Independent SQLite persistence for republish templates and jobs."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator, Optional, Sequence

from republish_models import (
    RepublishJob,
    RepublishTemplate,
    strict_json_loads,
    valid_job_statuses,
)


DEFAULT_DB_PATH = os.path.join("data", "xianyu_data.db")


class RepublishStoreError(RuntimeError):
    """Raised when the republish store cannot complete a database operation."""


class RepublishTemplateConflictError(RepublishStoreError):
    """Raised when an atomic template create hits an existing item mapping."""


class RepublishSourceItemMismatch(RepublishStoreError):
    """The delivery source is not the template's current item snapshot."""


class RepublishConcurrentRotationError(RepublishStoreError):
    """The template changed after a job was claimed."""


class RepublishStore:
    def __init__(self, db_path: os.PathLike[str] | str = DEFAULT_DB_PATH):
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def __enter__(self) -> "RepublishStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        """Keep the API context-manager friendly; connections are per operation."""

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: Optional[sqlite3.Connection] = None
        try:
            connection = sqlite3.connect(
                self.db_path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
        except sqlite3.Error as exc:
            raise RepublishStoreError(
                f"republish database operation failed for {self.db_path}: {exc}"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _now() -> float:
        return time.time()

    def ensure_schema(self) -> None:
        try:
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS republish_templates (
                        template_id TEXT PRIMARY KEY,
                        cookie_id TEXT NOT NULL,
                        current_item_id TEXT NOT NULL,
                        template_json TEXT NOT NULL,
                        delivery_content TEXT,
                        sku_delivery_json TEXT,
                        auto_delivery INTEGER NOT NULL DEFAULT 0,
                        auto_republish INTEGER NOT NULL DEFAULT 0,
                        paused INTEGER NOT NULL DEFAULT 0,
                        last_status TEXT NOT NULL DEFAULT 'ready',
                        last_error TEXT,
                        last_success_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(cookie_id, current_item_id)
                    );

                    CREATE TABLE IF NOT EXISTS republish_jobs (
                        job_id TEXT PRIMARY KEY,
                        template_id TEXT NOT NULL,
                        source_item_id TEXT NOT NULL,
                        trigger_order_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        available_at REAL NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        old_item_id TEXT NOT NULL,
                        new_item_id TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        order_context_json TEXT,
                        UNIQUE(template_id, trigger_order_id),
                        FOREIGN KEY(template_id) REFERENCES republish_templates(template_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_republish_jobs_due
                        ON republish_jobs(status, available_at, created_at);
                    CREATE INDEX IF NOT EXISTS idx_republish_jobs_template
                        ON republish_jobs(template_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_republish_jobs_trigger
                        ON republish_jobs(trigger_order_id);
                    CREATE INDEX IF NOT EXISTS idx_republish_templates_cookie
                        ON republish_templates(cookie_id, paused, updated_at);
                    """
                )
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(republish_jobs)").fetchall()
                }
                if "order_context_json" not in columns:
                    connection.execute(
                        "ALTER TABLE republish_jobs ADD COLUMN order_context_json TEXT"
                    )
        except RepublishStoreError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise RepublishStoreError(f"unable to create republish schema: {exc}") from exc

    @staticmethod
    def _template_json(template: RepublishTemplate) -> str:
        try:
            return json.dumps(
                template.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise RepublishStoreError("template contains non-JSON-serializable data") from exc

    @staticmethod
    def _template_from_row(row: sqlite3.Row) -> RepublishTemplate:
        try:
            payload = strict_json_loads(row["template_json"])
            if not isinstance(payload, dict):
                raise ValueError("template_json is not an object")
            payload.update(
                {
                    "template_id": row["template_id"],
                    "cookie_id": row["cookie_id"],
                    "current_item_id": row["current_item_id"],
                    "delivery_content": row["delivery_content"],
                    "sku_delivery": row["sku_delivery_json"] or {},
                    "auto_delivery": bool(row["auto_delivery"]),
                    "auto_republish": bool(row["auto_republish"]),
                    "paused": bool(row["paused"]),
                }
            )
            return RepublishTemplate.from_dict(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RepublishStoreError(
                f"invalid republish template data for {row['template_id']}"
            ) from exc

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> RepublishJob:
        try:
            return RepublishJob(
                job_id=row["job_id"],
                template_id=row["template_id"],
                source_item_id=row["source_item_id"],
                trigger_order_id=row["trigger_order_id"],
                status=row["status"],
                available_at=row["available_at"],
                attempts=row["attempts"],
                last_error=row["last_error"],
                old_item_id=row["old_item_id"],
                new_item_id=row["new_item_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                order_context=(
                    None
                    if row["order_context_json"] is None
                    else strict_json_loads(row["order_context_json"])
                ),
            )
        except (TypeError, ValueError) as exc:
            raise RepublishStoreError(f"invalid republish job data for {row['job_id']}") from exc

    @staticmethod
    def _begin(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _finish(connection: sqlite3.Connection, success: bool) -> None:
        if success:
            connection.commit()
        else:
            connection.rollback()

    def upsert_template(self, template: RepublishTemplate) -> RepublishTemplate:
        if not isinstance(template, RepublishTemplate):
            raise TypeError("template must be a RepublishTemplate")
        now = self._now()
        template_json = self._template_json(template)
        with self._connection() as connection:
            self._begin(connection)
            success = False
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM republish_templates
                    WHERE cookie_id = ? AND current_item_id = ?
                    """,
                    (template.cookie_id, template.current_item_id),
                ).fetchone()
                id_row = connection.execute(
                    "SELECT template_id FROM republish_templates WHERE template_id = ?",
                    (template.template_id,),
                ).fetchone()
                if existing is not None:
                    template_id = existing["template_id"]
                    persisted = replace(template, template_id=template_id)
                    template_json = self._template_json(persisted)
                    connection.execute(
                        """
                        UPDATE republish_templates
                        SET template_json = ?, delivery_content = ?, sku_delivery_json = ?,
                            auto_delivery = ?, auto_republish = ?, paused = ?, updated_at = ?
                        WHERE template_id = ?
                        """,
                        (
                            template_json,
                            persisted.delivery_content,
                            json.dumps(persisted.sku_delivery, ensure_ascii=False, allow_nan=False),
                            int(persisted.auto_delivery),
                            int(persisted.auto_republish),
                            int(persisted.paused),
                            now,
                            template_id,
                        ),
                    )
                else:
                    if id_row is not None:
                        raise ValueError(
                            f"template_id {template.template_id!r} already belongs to another item"
                        )
                    connection.execute(
                        """
                        INSERT INTO republish_templates (
                            template_id, cookie_id, current_item_id, template_json,
                            delivery_content, sku_delivery_json, auto_delivery,
                            auto_republish, paused, last_status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)
                        """,
                        (
                            template.template_id,
                            template.cookie_id,
                            template.current_item_id,
                            template_json,
                            template.delivery_content,
                            json.dumps(template.sku_delivery, ensure_ascii=False, allow_nan=False),
                            int(template.auto_delivery),
                            int(template.auto_republish),
                            int(template.paused),
                            now,
                            now,
                        ),
                    )
                result = connection.execute(
                    "SELECT * FROM republish_templates WHERE template_id = ?",
                    (template.template_id if existing is None else existing["template_id"],),
                ).fetchone()
                self._finish(connection, True)
                success = True
                return self._template_from_row(result)
            finally:
                if not success:
                    self._finish(connection, False)

    def create_template(self, template: RepublishTemplate) -> RepublishTemplate:
        """Atomically insert a new template; never overwrite an existing mapping."""
        if not isinstance(template, RepublishTemplate):
            raise TypeError("template must be a RepublishTemplate")
        now = self._now()
        template_json = self._template_json(template)
        sku_delivery_json = json.dumps(
            template.sku_delivery, ensure_ascii=False, allow_nan=False
        )
        with self._connection() as connection:
            self._begin(connection)
            success = False
            try:
                connection.execute(
                    """
                    INSERT INTO republish_templates (
                        template_id, cookie_id, current_item_id, template_json,
                        delivery_content, sku_delivery_json, auto_delivery,
                        auto_republish, paused, last_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)
                    """,
                    (
                        template.template_id,
                        template.cookie_id,
                        template.current_item_id,
                        template_json,
                        template.delivery_content,
                        sku_delivery_json,
                        int(template.auto_delivery),
                        int(template.auto_republish),
                        int(template.paused),
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM republish_templates WHERE template_id = ?",
                    (template.template_id,),
                ).fetchone()
                self._finish(connection, True)
                success = True
                return self._template_from_row(row)
            except sqlite3.IntegrityError as exc:
                self._finish(connection, False)
                success = True
                raise RepublishTemplateConflictError(
                    "template mapping already exists"
                ) from exc
            finally:
                if not success:
                    self._finish(connection, False)

    def get_template(
        self,
        template_id: Optional[str] = None,
        *,
        cookie_id: Optional[str] = None,
        current_item_id: Optional[str] = None,
    ) -> Optional[RepublishTemplate]:
        if template_id is None and (cookie_id is None or current_item_id is None):
            raise ValueError("provide template_id or both cookie_id and current_item_id")
        with self._connection() as connection:
            if template_id is not None:
                query = "SELECT * FROM republish_templates WHERE template_id = ?"
                params = (template_id,)
            else:
                query = """
                    SELECT * FROM republish_templates
                    WHERE cookie_id = ? AND current_item_id = ?
                """
                params = (cookie_id, current_item_id)
            row = connection.execute(query, params).fetchone()
            return None if row is None else self._template_from_row(row)

    def list_templates(
        self,
        cookie_id: Optional[str] = None,
        *,
        include_paused: bool = True,
    ) -> list[RepublishTemplate]:
        with self._connection() as connection:
            query = "SELECT * FROM republish_templates"
            params: list[object] = []
            clauses = []
            if cookie_id is not None:
                clauses.append("cookie_id = ?")
                params.append(cookie_id)
            if not include_paused:
                clauses.append("paused = 0")
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY updated_at DESC, template_id"
            return [self._template_from_row(row) for row in connection.execute(query, params)]

    def enqueue_after_delivery(
        self,
        template_id: str,
        source_item_id: str,
        trigger_order_id: str,
        available_at: Optional[float] = None,
        order_context=None,
        *,
        allow_paused: bool = False,
    ) -> Optional[RepublishJob]:
        template_id = self._required_id(template_id, "template_id")
        source_item_id = self._required_id(source_item_id, "source_item_id")
        trigger_order_id = self._required_id(trigger_order_id, "trigger_order_id")
        available_at = self._due_time(available_at, "available_at")
        try:
            order_context_json = (
                None
                if order_context is None
                else json.dumps(order_context, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("order_context must be strict JSON") from exc
        with self._connection() as connection:
            self._begin(connection)
            success = False
            try:
                existing = connection.execute(
                    "SELECT * FROM republish_templates WHERE template_id = ?",
                    (template_id,),
                ).fetchone()
                if existing is None:
                    raise ValueError(f"unknown template_id: {template_id}")
                if source_item_id != existing["current_item_id"]:
                    raise RepublishSourceItemMismatch(
                        "source_item_id does not match template current_item_id"
                    )
                existing_job = connection.execute(
                    """
                    SELECT * FROM republish_jobs
                    WHERE template_id = ? AND trigger_order_id = ?
                    """,
                    (template_id, trigger_order_id),
                ).fetchone()
                if existing_job is not None:
                    result = self._job_from_row(existing_job)
                else:
                    if bool(existing["paused"]) and not allow_paused:
                        self._finish(connection, True)
                        success = True
                        return None
                    now = self._now()
                    job_id = str(uuid.uuid4())
                    connection.execute(
                        """
                        INSERT INTO republish_jobs (
                            job_id, template_id, source_item_id, trigger_order_id,
                            status, available_at, attempts, old_item_id, created_at, updated_at,
                            order_context_json
                        ) VALUES (?, ?, ?, ?, 'pending', ?, 0, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            template_id,
                            source_item_id,
                            trigger_order_id,
                            available_at,
                            existing["current_item_id"],
                            now,
                            now,
                            order_context_json,
                        ),
                    )
                    result = self._job_from_row(
                        connection.execute(
                            "SELECT * FROM republish_jobs WHERE job_id = ?", (job_id,)
                        ).fetchone()
                    )
                self._finish(connection, True)
                success = True
                return result
            finally:
                if not success:
                    self._finish(connection, False)

    def claim_due_job(self, now: Optional[float] = None) -> Optional[RepublishJob]:
        now = self._due_time(now, "now")
        with self._connection() as connection:
            self._begin(connection)
            success = False
            try:
                row = connection.execute(
                    """
                    SELECT j.*
                    FROM republish_jobs AS j
                    JOIN republish_templates AS t ON t.template_id = j.template_id
                    WHERE j.status IN ('pending', 'retry')
                      AND j.available_at <= ?
                      AND NOT EXISTS (
                          SELECT 1 FROM republish_jobs AS running
                          WHERE running.template_id = j.template_id
                            AND running.status = 'running'
                      )
                    ORDER BY j.available_at ASC, j.created_at ASC
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone()
                if row is None:
                    self._finish(connection, True)
                    success = True
                    return None
                changed = connection.execute(
                    """
                    UPDATE republish_jobs
                    SET status = 'running', updated_at = ?
                    WHERE job_id = ? AND status IN ('pending', 'retry')
                    """,
                    (now, row["job_id"]),
                ).rowcount
                if changed != 1:
                    self._finish(connection, True)
                    success = True
                    return None
                claimed = connection.execute(
                    "SELECT * FROM republish_jobs WHERE job_id = ?", (row["job_id"],)
                ).fetchone()
                self._finish(connection, True)
                success = True
                return self._job_from_row(claimed)
            finally:
                if not success:
                    self._finish(connection, False)

    def mark_succeeded(
        self, job_id: str, new_item_id: str, now: Optional[float] = None
    ) -> RepublishJob:
        job_id = self._required_id(job_id, "job_id")
        new_item_id = self._required_id(new_item_id, "new_item_id")
        now = self._due_time(now, "now")
        with self._connection() as connection:
            self._begin(connection)
            success = False
            try:
                row = connection.execute(
                    "SELECT * FROM republish_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown job_id: {job_id}")
                if row["status"] != "running":
                    raise ValueError(f"job {job_id} is not running")
                if new_item_id in {row["old_item_id"], row["source_item_id"]}:
                    raise ValueError(
                        "new_item_id must differ from source_item_id and old_item_id"
                    )
                template_row = connection.execute(
                    "SELECT * FROM republish_templates WHERE template_id = ?",
                    (row["template_id"],),
                ).fetchone()
                if template_row is None:
                    raise RepublishStoreError(f"job {job_id} references a missing template")
                if template_row["current_item_id"] != row["old_item_id"]:
                    raise RepublishConcurrentRotationError(
                        "template current_item_id changed after job claim"
                    )
                conflict = connection.execute(
                    """
                    SELECT template_id FROM republish_templates
                    WHERE cookie_id = ? AND current_item_id = ? AND template_id <> ?
                    """,
                    (template_row["cookie_id"], new_item_id, row["template_id"]),
                ).fetchone()
                if conflict is not None:
                    raise ValueError(
                        f"new_item_id {new_item_id!r} is already linked to template {conflict['template_id']}"
                    )
                template = self._template_from_row(template_row)
                updated_template = replace(template, current_item_id=new_item_id)
                connection.execute(
                    """
                    UPDATE republish_templates
                    SET current_item_id = ?, template_json = ?, last_status = 'succeeded',
                        last_error = NULL, last_success_at = ?, updated_at = ?
                    WHERE template_id = ?
                    """,
                    (
                        new_item_id,
                        self._template_json(updated_template),
                        now,
                        now,
                        row["template_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE republish_jobs
                    SET status = 'succeeded', new_item_id = ?, last_error = NULL, updated_at = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (new_item_id, now, job_id),
                )
                result = self._job_from_row(
                    connection.execute("SELECT * FROM republish_jobs WHERE job_id = ?", (job_id,)).fetchone()
                )
                self._finish(connection, True)
                success = True
                return result
            finally:
                if not success:
                    self._finish(connection, False)

    def mark_retry(
        self,
        job_id: str,
        available_at: float,
        error: Optional[str] = None,
        attempts: Optional[int] = None,
        *,
        last_error: Optional[str] = None,
        now: Optional[float] = None,
    ) -> RepublishJob:
        job_id = self._required_id(job_id, "job_id")
        available_at = self._due_time(available_at, "available_at")
        if error is not None and last_error is not None:
            raise ValueError("provide error or last_error, not both")
        error = error if error is not None else last_error
        if not isinstance(error, str) or not error.strip():
            raise ValueError("retry error must be a non-empty string")
        now = self._due_time(now, "now")
        with self._connection() as connection:
            self._begin(connection)
            success = False
            try:
                row = self._running_job(connection, job_id)
                next_attempts = row["attempts"] + 1 if attempts is None else attempts
                if isinstance(next_attempts, bool) or not isinstance(next_attempts, int) or next_attempts < 1:
                    raise ValueError("attempts must be a positive integer")
                connection.execute(
                    """
                    UPDATE republish_jobs
                    SET status = 'retry', available_at = ?, attempts = ?, last_error = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (available_at, next_attempts, error, now, job_id),
                )
                connection.execute(
                    """
                    UPDATE republish_templates
                    SET last_status = 'retry', last_error = ?, updated_at = ?
                    WHERE template_id = ?
                    """,
                    (error, now, row["template_id"]),
                )
                result = self._job_from_row(
                    connection.execute("SELECT * FROM republish_jobs WHERE job_id = ?", (job_id,)).fetchone()
                )
                self._finish(connection, True)
                success = True
                return result
            finally:
                if not success:
                    self._finish(connection, False)

    def mark_manual_required(
        self,
        job_id: str,
        error: Optional[str] = None,
        *,
        last_error: Optional[str] = None,
        now: Optional[float] = None,
    ) -> RepublishJob:
        job_id = self._required_id(job_id, "job_id")
        if error is not None and last_error is not None:
            raise ValueError("provide error or last_error, not both")
        error = error if error is not None else last_error
        if not isinstance(error, str) or not error.strip():
            raise ValueError("manual processing error must be a non-empty string")
        now = self._due_time(now, "now")
        with self._connection() as connection:
            self._begin(connection)
            success = False
            try:
                row = self._running_job(connection, job_id)
                connection.execute(
                    """
                    UPDATE republish_jobs
                    SET status = 'manual_required', last_error = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (error, now, job_id),
                )
                connection.execute(
                    """
                    UPDATE republish_templates
                    SET last_status = 'manual_required', last_error = ?, updated_at = ?
                    WHERE template_id = ?
                    """,
                    (error, now, row["template_id"]),
                )
                result = self._job_from_row(
                    connection.execute("SELECT * FROM republish_jobs WHERE job_id = ?", (job_id,)).fetchone()
                )
                self._finish(connection, True)
                success = True
                return result
            finally:
                if not success:
                    self._finish(connection, False)

    def list_jobs(
        self,
        template_id: Optional[str] = None,
        *,
        statuses: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
    ) -> list[RepublishJob]:
        if statuses is not None:
            statuses = tuple(statuses)
            invalid = set(statuses) - set(valid_job_statuses())
            if invalid:
                raise ValueError(f"unsupported job status: {sorted(invalid)}")
        if limit is not None and (isinstance(limit, bool) or limit < 1):
            raise ValueError("limit must be a positive integer")
        with self._connection() as connection:
            clauses = []
            params: list[object] = []
            if template_id is not None:
                clauses.append("template_id = ?")
                params.append(self._required_id(template_id, "template_id"))
            if statuses:
                clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
                params.extend(statuses)
            query = "SELECT * FROM republish_jobs"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY created_at ASC, job_id"
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            return [self._job_from_row(row) for row in connection.execute(query, params)]

    def list_recent_jobs(self, cookie_id: str, *, limit: int = 50) -> list[RepublishJob]:
        """Return bounded recent jobs for one cookie with a single joined query."""
        cookie_id = self._required_id(cookie_id, "cookie_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT j.*
                FROM republish_jobs AS j
                JOIN republish_templates AS t ON t.template_id = j.template_id
                WHERE t.cookie_id = ?
                ORDER BY j.updated_at DESC, j.job_id DESC
                LIMIT ?
                """,
                (cookie_id, limit),
            ).fetchall()
            return [self._job_from_row(row) for row in rows]

    def list_latest_jobs_by_template_ids(
        self, template_ids: Sequence[str]
    ) -> dict[str, RepublishJob]:
        """Return the latest job for each requested template with one query."""
        normalized_ids = [self._required_id(template_id, "template_id") for template_id in template_ids]
        if not normalized_ids:
            return {}
        placeholders = ",".join("?" for _ in normalized_ids)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                WITH ranked_jobs AS (
                    SELECT j.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY j.template_id
                               ORDER BY j.updated_at DESC, j.created_at DESC, j.job_id DESC
                           ) AS row_number
                    FROM republish_jobs AS j
                    WHERE j.template_id IN ({placeholders})
                )
                SELECT job_id, template_id, source_item_id, trigger_order_id, status,
                       available_at, attempts, last_error, old_item_id, new_item_id,
                       created_at, updated_at, order_context_json
                FROM ranked_jobs
                WHERE row_number = 1
                """,
                normalized_ids,
            ).fetchall()
            return {row["template_id"]: self._job_from_row(row) for row in rows}

    @staticmethod
    def _required_id(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _due_time(value: Optional[float], field_name: str) -> float:
        if value is None:
            return time.time()
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be a timestamp")
        try:
            timestamp = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be a timestamp") from exc
        if not math.isfinite(timestamp):
            raise ValueError(f"{field_name} must be finite")
        return timestamp

    @staticmethod
    def _running_job(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM republish_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown job_id: {job_id}")
        if row["status"] != "running":
            raise ValueError(f"job {job_id} is not running")
        return row
