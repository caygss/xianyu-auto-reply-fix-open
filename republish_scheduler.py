"""A single asyncio worker for durable republish jobs."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional


class RepublishScheduler:
    def __init__(
        self,
        coordinator: Any,
        *,
        interval: float = 30.0,
        event: Optional[asyncio.Event] = None,
        clock: Callable[[], float] = time.monotonic,
        error_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        if interval < 0:
            raise ValueError("interval must be non-negative")
        self.coordinator = coordinator
        self.interval = float(interval)
        self.event = event
        self.clock = clock
        self.error_callback = error_callback
        self._stop_event = asyncio.Event()
        self._cycle_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._run_lock = asyncio.Lock()
        self._next_run_at: Optional[float] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def next_run_at(self) -> Optional[float]:
        return self._next_run_at

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._task is not None and self._task.done():
            finished_task = self._task
            self._task = None
            try:
                finished_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self._record_error(exc)
        self._stop_event = asyncio.Event()
        self._cycle_event = asyncio.Event()
        self._next_run_at = self.clock()
        self._task = asyncio.create_task(self._worker(), name="republish-scheduler")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop_event.set()
        if self.event is not None:
            self.event.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def wait_for_cycle(self) -> None:
        if self._cycle_event.is_set():
            self._cycle_event.clear()
            return
        if self._task is None or self._task.done():
            return
        await self._cycle_event.wait()
        self._cycle_event.clear()

    async def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                async with self._run_lock:
                    if self._stop_event.is_set():
                        break
                    await self.coordinator.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(exc)
            try:
                self._next_run_at = self.clock() + self.interval
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._next_run_at = None
                self._record_error(exc)
            finally:
                self._cycle_event.set()
            if self._stop_event.is_set():
                break
            try:
                await self._wait_for_next_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(exc)
                self._cycle_event.set()
                await asyncio.sleep(0)

    def _record_error(self, error: Exception) -> None:
        if self.error_callback is None:
            return
        try:
            self.error_callback(f"scheduler_error:{type(error).__name__}")
        except Exception:
            pass

    async def _wait_for_next_cycle(self) -> None:
        deadline = self._next_run_at if self._next_run_at is not None else self.clock()
        while not self._stop_event.is_set():
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            waiters = []
            try:
                waiters.append(
                    asyncio.create_task(
                        self._stop_event.wait(), name="republish-scheduler-waiter-stop"
                    )
                )
                if self.event is not None:
                    waiters.append(
                        asyncio.create_task(
                            self.event.wait(), name="republish-scheduler-waiter-event"
                        )
                    )
                done, _ = await asyncio.wait(
                    waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                for waiter in done:
                    if not waiter.cancelled():
                        error = waiter.exception()
                        if error is not None:
                            raise error
            finally:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
            if self.event is not None and self.event.is_set():
                self.event.clear()
            if done:
                return
