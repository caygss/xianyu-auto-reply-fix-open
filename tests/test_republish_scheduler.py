import asyncio

from republish_scheduler import RepublishScheduler


class FakeClock:
    def __init__(self, value=100.0):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


class BlockingCoordinator:
    def __init__(self):
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_once(self):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        await self.release.wait()
        self.active -= 1
        return None


def test_scheduler_start_stop_uses_one_worker_and_does_not_parallelize():
    async def scenario():
        coordinator = BlockingCoordinator()
        scheduler = RepublishScheduler(coordinator, interval=0.001)

        await scheduler.start()
        await asyncio.wait_for(coordinator.started.wait(), timeout=1)
        await asyncio.sleep(0.01)
        assert coordinator.calls == 1
        assert coordinator.max_active == 1

        coordinator.release.set()
        await scheduler.stop()
        assert scheduler.running is False
        assert coordinator.active == 0

    asyncio.run(scenario())


def test_scheduler_can_wait_for_one_cycle_and_stop_without_leaked_task():
    async def scenario():
        calls = 0

        class Coordinator:
            async def run_once(self):
                nonlocal calls
                calls += 1
                return None

        scheduler = RepublishScheduler(Coordinator(), interval=60)
        await scheduler.start()
        await asyncio.wait_for(scheduler.wait_for_cycle(), timeout=1)
        await scheduler.stop()
        assert calls == 1
        assert scheduler.running is False

    asyncio.run(scenario())


def test_scheduler_uses_injected_clock_to_schedule_next_cycle():
    async def scenario():
        clock = FakeClock(100.0)
        cycle_done = asyncio.Event()

        class Coordinator:
            async def run_once(self):
                cycle_done.set()
                return None

        scheduler = RepublishScheduler(Coordinator(), interval=30, clock=clock)
        await scheduler.start()
        await asyncio.wait_for(cycle_done.wait(), timeout=1)
        await asyncio.sleep(0)

        assert scheduler.next_run_at == 130.0
        assert clock.calls > 0

        await scheduler.stop()

    asyncio.run(scenario())


def test_scheduler_records_run_once_exception_signals_cycle_and_continues():
    async def scenario():
        errors = []
        calls = 0
        second_cycle = asyncio.Event()

        class Coordinator:
            async def run_once(self):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ValueError("private scheduler failure")
                second_cycle.set()
                return None

        scheduler = RepublishScheduler(
            Coordinator(), interval=0.001, error_callback=errors.append
        )
        await scheduler.start()
        await asyncio.wait_for(scheduler.wait_for_cycle(), timeout=1)
        assert errors == ["scheduler_error:ValueError"]
        await asyncio.wait_for(second_cycle.wait(), timeout=1)
        assert calls >= 2
        assert scheduler.running is True
        await scheduler.stop()
        assert scheduler.running is False

    asyncio.run(scenario())


def test_scheduler_stop_converges_when_run_once_is_cancelled():
    async def scenario():
        started = asyncio.Event()

        class Coordinator:
            async def run_once(self):
                started.set()
                await asyncio.Event().wait()

        scheduler = RepublishScheduler(Coordinator(), interval=60)
        await scheduler.start()
        await asyncio.wait_for(started.wait(), timeout=1)

        task = scheduler._task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await scheduler.stop()

        assert scheduler.running is False

    asyncio.run(scenario())


def test_scheduler_waiter_tasks_are_cleaned_when_wait_is_cancelled():
    async def scenario():
        scheduler = RepublishScheduler(object(), interval=60)
        wait_task = asyncio.create_task(scheduler._wait_for_next_cycle())
        await asyncio.sleep(0)
        wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)

        leaked = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("republish-scheduler-waiter")
        ]
        assert leaked == []

    asyncio.run(scenario())


def test_scheduler_can_restart_after_external_worker_cancellation():
    async def scenario():
        scheduler = RepublishScheduler(object(), interval=60)
        await scheduler.start()
        first_task = scheduler._task
        first_task.cancel()
        await asyncio.gather(first_task, return_exceptions=True)

        await scheduler.start()
        second_task = scheduler._task
        assert second_task is not first_task
        assert scheduler.running is True

        await scheduler.stop()

    asyncio.run(scenario())


def test_scheduler_recovers_clock_wait_error_and_signals_cycle():
    async def scenario():
        errors = []
        clock_calls = 0

        def clock():
            nonlocal clock_calls
            clock_calls += 1
            if clock_calls == 3:
                raise RuntimeError("private clock failure")
            return 0.0

        class Coordinator:
            async def run_once(self):
                return None

        scheduler = RepublishScheduler(
            Coordinator(), interval=60, clock=clock, error_callback=errors.append
        )
        await scheduler.start()
        await asyncio.wait_for(scheduler.wait_for_cycle(), timeout=1)
        await asyncio.wait_for(scheduler.wait_for_cycle(), timeout=1)

        assert errors == ["scheduler_error:RuntimeError"]
        await scheduler.stop()

    asyncio.run(scenario())


def test_scheduler_recovers_event_wait_error_and_cleans_waiters():
    async def scenario():
        errors = []

        class ExplodingEvent:
            def is_set(self):
                return False

            def clear(self):
                return None

            def set(self):
                return None

            async def wait(self):
                raise LookupError("private event failure")

        class Coordinator:
            async def run_once(self):
                return None

        scheduler = RepublishScheduler(
            Coordinator(), interval=60, event=ExplodingEvent(), error_callback=errors.append
        )
        await scheduler.start()
        await asyncio.wait_for(scheduler.wait_for_cycle(), timeout=1)
        await asyncio.wait_for(scheduler.wait_for_cycle(), timeout=1)

        assert errors == ["scheduler_error:LookupError"]
        leaked = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("republish-scheduler-waiter")
        ]
        assert leaked == []
        await scheduler.stop()

    asyncio.run(scenario())


def test_scheduler_start_consumes_exception_from_finished_task_before_restart():
    async def scenario():
        errors = []

        async def failed_worker():
            raise RuntimeError("private finished task failure")

        scheduler = RepublishScheduler(object(), error_callback=errors.append)
        failed_task = asyncio.create_task(failed_worker())
        await asyncio.sleep(0)
        scheduler._task = failed_task

        await scheduler.start()

        assert errors == ["scheduler_error:RuntimeError"]
        assert scheduler._task is not failed_task
        assert scheduler.running is True
        await scheduler.stop()

    asyncio.run(scenario())
