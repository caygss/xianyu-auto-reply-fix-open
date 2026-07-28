"""Retryable, idempotent coordination for post-delivery republishing.

The coordinator contains no Xianyu session or network code.  External effects
are represented by small ports so the application entry points can provide
their own adapters later.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Sequence
from urllib.parse import urlparse

from republish_models import RepublishJob, RepublishTemplate
from republish_store import RepublishStore
from republish_template_service import resolve_delivery_content, safe_delivery_summary


class ItemAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


AvailabilityStatus = ItemAvailability
Availability = ItemAvailability


class PublisherPort(Protocol):
    async def publish(self, template: RepublishTemplate) -> Optional[str]:
        """Publish a copy and return its new item id."""


class ItemAvailabilityPort(Protocol):
    async def check(self, cookie_id: str, item_id: str) -> ItemAvailability:
        """Return the source item's current availability."""


class ManualActionRequired(RuntimeError):
    """An expected condition that must be resolved by a human."""

    def __init__(self, reason: str = "manual_action_required") -> None:
        self.reason_code = str(reason).strip() or "manual_action_required"
        super().__init__(self.reason_code)


class RepublishConfigurationError(ManualActionRequired):
    """Template/material data is invalid and requires manual correction."""


class InvalidPublishedItemId(RuntimeError):
    """Publisher returned an unusable or non-new item id."""


class CallableItemAvailabilityAdapter:
    """Adapt either a synchronous or asynchronous availability callable."""

    def __init__(self, checker: Callable[[str, str], Any]):
        self._checker = checker

    async def check(self, cookie_id: str, item_id: str) -> ItemAvailability:
        value = self._checker(cookie_id, item_id)
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, ItemAvailability):
            return value
        try:
            return ItemAvailability(str(value).strip().upper())
        except ValueError as exc:
            raise ValueError("availability callable returned an unsupported state") from exc


class ItemPublisherAdapter:
    """Adapt an existing ``ItemPublisher`` instance without creating a session."""

    def __init__(self, publisher: Any):
        self._publisher = publisher

    async def publish(self, template: RepublishTemplate) -> Optional[str]:
        if getattr(self._publisher, "cookie_id", None) != template.cookie_id:
            raise RepublishConfigurationError("publisher cookie does not match template cookie")
        payload = self._publisher.publish_item(
            title=template.title,
            description=template.description,
            images=[{"url": image} for image in template.images],
            current_price=template.current_price,
            original_price=template.original_price,
            delivery_choice=template.delivery_choice or "",
            post_price=template.post_price,
            can_self_pickup=template.can_self_pickup,
            category_hint=template.category_hint,
        )
        if inspect.isawaitable(payload):
            payload = await payload
        extractor = getattr(self._publisher, "extract_published_item_id", None)
        if extractor is None:
            extractor = getattr(type(self._publisher), "extract_published_item_id", None)
        if extractor is None:
            return None
        extracted = extractor(payload) if isinstance(payload, dict) else None
        if inspect.isawaitable(extracted):
            extracted = await extracted
        return extracted


@dataclass(frozen=True)
class RepublishOutcome:
    status: str
    job_id: Optional[str] = None
    reason: Optional[str] = None
    new_item_id: Optional[str] = None
    safe_delivery_summary: str = "delivery:unresolved"


LogCallback = Callable[[Mapping[str, str]], None]


class RepublishCoordinator:
    """Own the durable state machine for one republish job at a time."""

    def __init__(
        self,
        store: RepublishStore,
        publisher: PublisherPort,
        availability: ItemAvailabilityPort,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        retry_backoff_seconds: Sequence[float] = (300.0, 900.0, 1800.0),
        dry_run: bool = False,
        log_callback: Optional[LogCallback] = None,
    ) -> None:
        if not isinstance(store, RepublishStore):
            raise TypeError("store must be a RepublishStore")
        backoff = tuple(float(value) for value in retry_backoff_seconds)
        if len(backoff) != 3 or any(value < 0 for value in backoff):
            raise ValueError("retry_backoff_seconds must contain three non-negative values")
        self.store = store
        self.publisher = publisher
        self.availability = availability
        self.clock = clock
        self.sleep = sleep
        self.retry_backoff_seconds = backoff
        self.dry_run = bool(dry_run)
        self.log_callback = log_callback

    def on_delivery_finalized(
        self,
        order_id: str,
        cookie_id: str,
        item_id: str,
        order_context: Any = None,
    ) -> RepublishOutcome:
        """Enqueue only after the caller has confirmed successful delivery."""

        template = self.store.get_template(cookie_id=cookie_id, current_item_id=item_id)
        if template is None:
            return RepublishOutcome(status="ignored", reason="template_not_configured")
        job = self.store.enqueue_after_delivery(
            template.template_id,
            item_id,
            order_id,
            available_at=self.clock(),
            order_context=order_context,
            allow_paused=True,
        )
        if job is None:
            return RepublishOutcome(status="ignored", reason="template_not_configured")
        return RepublishOutcome(status="enqueued", job_id=job.job_id)

    async def run_once(self) -> Optional[RepublishOutcome]:
        job = self.store.claim_due_job(now=self.clock())
        if job is None:
            return None
        try:
            prepared = self._prepare_claimed_job(job)
        except Exception as exc:
            return self._recover_claimed_job(job, f"template_read_error:{type(exc).__name__}")
        if isinstance(prepared, RepublishOutcome):
            return prepared
        template, delivery_summary = prepared

        try:
            source_state = await self._maybe_await(
                self.availability.check(template.cookie_id, job.old_item_id)
            )
            source_state = self._normalize_availability(source_state)
            if source_state is not ItemAvailability.UNAVAILABLE:
                return self._retry(job, "source_not_confirmed_unavailable", delivery_summary)
            self._log(job, delivery_summary)
            if self.dry_run:
                retry_outcome = self._retry_or_manual(
                    job, "dry_run_preview", delivery_summary
                )
                if retry_outcome.status == "manual_required":
                    return retry_outcome
                return RepublishOutcome(
                    status="preview",
                    job_id=job.job_id,
                    reason="dry_run_preview",
                    safe_delivery_summary=delivery_summary,
                )

            try:
                published_id = await self._maybe_await(self.publisher.publish(template))
                self._validate_published_item_id(published_id, job)
            except InvalidPublishedItemId:
                return self._retry_or_manual(job, "published_item_id_invalid", delivery_summary)
            except RepublishConfigurationError:
                return self._manual(job, "template_config_invalid", delivery_summary)
            try:
                succeeded = self.store.mark_succeeded(
                    job.job_id, published_id.strip(), now=self.clock()
                )
            except Exception:
                return self._manual(job, "state_update_failed", delivery_summary)
            return RepublishOutcome(
                status="succeeded",
                job_id=succeeded.job_id,
                new_item_id=succeeded.new_item_id,
                safe_delivery_summary=delivery_summary,
            )
        except ManualActionRequired as exc:
            return self._manual(job, "manual_action_required", delivery_summary)
        except Exception as exc:
            return self._retry_or_manual(job, self._safe_exception_code(exc), delivery_summary)

    def _prepare_claimed_job(
        self, job: RepublishJob
    ) -> tuple[RepublishTemplate, str] | RepublishOutcome:
        template = self.store.get_template(template_id=job.template_id)
        if template is None:
            return self._manual(job, "template_missing")
        if template.current_item_id != job.old_item_id:
            return self._manual(job, "template_item_snapshot_mismatch")
        if template.paused:
            return self._manual(job, "template_paused")
        if not template.auto_republish:
            return self._manual(job, "auto_republish_disabled")
        if not template.auto_delivery:
            return self._manual(job, "auto_delivery_disabled")
        try:
            self._validate_template_configuration(template)
        except RepublishConfigurationError:
            return self._manual(job, "template_config_invalid")
        try:
            delivery_content = resolve_delivery_content(template, job.order_context or {})
        except Exception:
            return self._manual(job, "delivery_content_unresolved")
        return template, safe_delivery_summary(delivery_content)

    def _recover_claimed_job(self, job: RepublishJob, reason: str) -> RepublishOutcome:
        try:
            self._log(job, "delivery:unresolved")
        except Exception:
            pass
        try:
            return self._retry_or_manual(job, reason, "delivery:unresolved")
        except Exception:
            try:
                return self._manual(job, "claim_recovery_failed", "delivery:unresolved")
            except Exception:
                return RepublishOutcome(
                    status="manual_required",
                    job_id=job.job_id,
                    reason="claim_recovery_failed",
                )

    @staticmethod
    def _validate_template_configuration(template: RepublishTemplate) -> None:
        if not isinstance(template.title, str) or not template.title.strip():
            raise RepublishConfigurationError("template title is invalid")
        if not isinstance(template.description, str) or not template.description.strip():
            raise RepublishConfigurationError("template description is invalid")
        if not isinstance(template.images, list) or not template.images:
            raise RepublishConfigurationError("template images are invalid")
        for image in template.images:
            if not isinstance(image, str) or not image.strip():
                raise RepublishConfigurationError("template image is invalid")
            parsed = urlparse(image.strip())
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                continue
            if parsed.scheme == "data" and image.strip().lower().startswith("data:image/"):
                continue
            raise RepublishConfigurationError("template image is invalid")
        if not isinstance(template.category_hint, (str, type(None))):
            raise RepublishConfigurationError("template category is invalid")
        if template.category_hint is not None and not template.category_hint.strip():
            raise RepublishConfigurationError("template category is invalid")
        if not isinstance(template.delivery_choice, str) or not template.delivery_choice.strip():
            raise RepublishConfigurationError("template delivery choice is invalid")

    @staticmethod
    def _validate_published_item_id(value: Any, job: RepublishJob) -> None:
        if not isinstance(value, str) or not value.strip():
            raise InvalidPublishedItemId("publisher returned no item id")
        normalized = value.strip()
        if normalized in {job.source_item_id, job.old_item_id}:
            raise InvalidPublishedItemId("publisher returned the source item id")

    @staticmethod
    def _normalize_availability(value: Any) -> ItemAvailability:
        if isinstance(value, ItemAvailability):
            return value
        try:
            return ItemAvailability(str(value).strip().upper())
        except ValueError:
            return ItemAvailability.UNKNOWN

    async def _maybe_await(self, value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    def _retry(
        self, job: RepublishJob, reason: str, delivery_summary: str
    ) -> RepublishOutcome:
        return self._retry_or_manual(job, reason, delivery_summary)

    def _retry_or_manual(
        self, job: RepublishJob, reason: str, delivery_summary: str
    ) -> RepublishOutcome:
        if job.attempts >= len(self.retry_backoff_seconds):
            return self._manual(job, "retry_limit_reached", delivery_summary)
        updated = self.store.mark_retry(
            job.job_id,
            available_at=self.clock() + self.retry_backoff_seconds[job.attempts],
            error=reason,
            now=self.clock(),
        )
        return RepublishOutcome(
            status="retry",
            job_id=updated.job_id,
            reason=reason,
            safe_delivery_summary=delivery_summary,
        )

    def _manual(
        self, job: RepublishJob, reason: str, delivery_summary: str = "delivery:unresolved"
    ) -> RepublishOutcome:
        updated = self.store.mark_manual_required(job.job_id, error=reason, now=self.clock())
        return RepublishOutcome(
            status="manual_required",
            job_id=updated.job_id,
            reason=reason,
            safe_delivery_summary=delivery_summary,
        )

    def _log(self, job: RepublishJob, delivery_summary: str) -> None:
        if self.log_callback is not None:
            self.log_callback(
                {
                    "template_id": job.template_id,
                    "item_id": job.old_item_id,
                    "job_id": job.job_id,
                    "safe_delivery_summary": delivery_summary,
                }
            )

    @staticmethod
    def _safe_exception_code(error: Exception) -> str:
        return f"publish_error:{type(error).__name__}"
