"""Queue semantics: retries, dead-lettering, and what the worker guarantees.

The backends need real brokers, so these tests run against an in-memory
backend that implements the same protocol. What is being tested here is the
contract and the worker loop — the parts every backend must agree on.
"""

from __future__ import annotations

import asyncio

import pytest

from jfastframework.queues.base import Job, QueueBackend
from jfastframework.queues.worker import TaskRegistry, UnknownTask, Worker


class MemoryQueue:
    """Protocol-conformant backend, with the same at-least-once semantics."""

    def __init__(self) -> None:
        self.pending: list[Job] = []
        self.inflight: dict[str, Job] = {}
        self.dead: list[Job] = []
        self.acked: list[Job] = []
        self.setup_called = False
        self.closed = False

    async def setup(self) -> None:
        self.setup_called = True

    async def enqueue(self, job: Job) -> str:
        self.pending.append(job)
        return job.id

    async def dequeue(self, *, timeout: float = 5.0) -> Job | None:
        if not self.pending:
            return None
        job = self.pending.pop(0)
        job.attempts += 1
        self.inflight[job.id] = job
        return job

    async def ack(self, job: Job) -> None:
        self.inflight.pop(job.id, None)
        self.acked.append(job)

    async def nack(self, job: Job, *, retry: bool = True) -> None:
        self.inflight.pop(job.id, None)
        if not retry or job.exhausted:
            self.dead.append(job)
        else:
            self.pending.append(job)

    async def stats(self) -> dict[str, int]:
        return {
            "pending": len(self.pending),
            "running": len(self.inflight),
            "dead": len(self.dead),
        }

    async def health(self) -> tuple[bool, str]:
        return True, "memory queue"

    async def close(self) -> None:
        self.closed = True


def test_the_memory_backend_satisfies_the_protocol() -> None:
    # If this fails, the test double has drifted from the contract and the
    # rest of this file is testing nothing real.
    assert isinstance(MemoryQueue(), QueueBackend)


# -- Job ----------------------------------------------------------------


def test_a_job_round_trips_through_json() -> None:
    job = Job(task="send_email", payload={"to": "a@b.c"}, request_id="trace-me")
    restored = Job.from_json(job.to_json())
    assert restored.id == job.id
    assert restored.task == "send_email"
    assert restored.payload == {"to": "a@b.c"}
    # Without this the job's logs cannot be tied to the request that queued it.
    assert restored.request_id == "trace-me"


def test_backoff_grows_and_is_capped() -> None:
    first = Job(task="t", attempts=1).backoff().total_seconds()
    later = Job(task="t", attempts=5).backoff().total_seconds()
    extreme = Job(task="t", attempts=50).backoff().total_seconds()

    assert later > first
    # Uncapped exponential backoff on attempt 50 schedules the retry past the
    # heat death of the deploy; the cap is what keeps it observable.
    assert extreme == 300.0


def test_a_job_is_exhausted_once_it_hits_max_attempts() -> None:
    assert not Job(task="t", attempts=2, max_attempts=3).exhausted
    assert Job(task="t", attempts=3, max_attempts=3).exhausted


# -- worker -------------------------------------------------------------


async def test_a_successful_job_is_acked() -> None:
    backend = MemoryQueue()
    registry = TaskRegistry()
    done = asyncio.Event()

    @registry.task("work")
    async def work(payload: dict) -> None:
        done.set()

    await backend.enqueue(Job(task="work"))
    handled = await Worker(backend, registry).run_once()

    assert handled and done.is_set()
    assert len(backend.acked) == 1
    assert backend.pending == []


async def test_a_failing_job_is_retried() -> None:
    backend = MemoryQueue()
    registry = TaskRegistry()

    @registry.task("boom")
    async def boom(payload: dict) -> None:
        raise RuntimeError("nope")

    await backend.enqueue(Job(task="boom", max_attempts=3))
    await Worker(backend, registry).run_once()

    assert backend.acked == []
    assert len(backend.pending) == 1
    assert backend.dead == []


async def test_a_job_that_exhausts_its_attempts_is_dead_lettered() -> None:
    backend = MemoryQueue()
    registry = TaskRegistry()

    @registry.task("boom")
    async def boom(payload: dict) -> None:
        raise RuntimeError("nope")

    await backend.enqueue(Job(task="boom", max_attempts=2))
    worker = Worker(backend, registry)
    while await worker.run_once():
        pass

    # Bounded retries: one poison message must not occupy a worker forever.
    assert len(backend.dead) == 1
    assert backend.dead[0].attempts == 2


async def test_an_unknown_task_is_dead_lettered_without_retrying() -> None:
    backend = MemoryQueue()
    await backend.enqueue(Job(task="nobody_registered_this", max_attempts=5))

    await Worker(backend, TaskRegistry()).run_once()

    # No future deploy makes this deliverable, so retrying it just hides the
    # real problem behind a growing pending queue.
    assert len(backend.dead) == 1
    assert backend.pending == []


async def test_a_timed_out_job_is_retried_not_lost() -> None:
    backend = MemoryQueue()
    registry = TaskRegistry()

    @registry.task("slow")
    async def slow(payload: dict) -> None:
        await asyncio.sleep(5)

    await backend.enqueue(Job(task="slow", max_attempts=3))
    await Worker(backend, registry, job_timeout=0.05).run_once()

    assert len(backend.pending) == 1
    assert backend.acked == []


async def test_run_once_reports_an_empty_queue() -> None:
    assert await Worker(MemoryQueue(), TaskRegistry()).run_once() is False


async def test_an_idle_worker_yields_instead_of_spinning() -> None:
    """A non-blocking backend must not turn the loop into a busy-wait.

    PostgreSQL polls: its dequeue returns instantly when the queue is empty.
    Without the idle wait, this loop never yields, burns a core, and starves
    every other coroutine in the process — including the HTTP handlers.
    """
    backend = MemoryQueue()
    worker = Worker(backend, TaskRegistry(), poll_timeout=0.05)
    task = asyncio.create_task(worker.run())

    ticks = 0
    for _ in range(20):
        await asyncio.sleep(0.005)
        ticks += 1

    worker.stop()
    await asyncio.wait_for(task, timeout=5)

    # If the worker had starved the loop, these sleeps would not have run.
    assert ticks == 20


async def test_stopping_an_idle_worker_is_immediate() -> None:
    # A worker that finishes its idle sleep before noticing the stop adds that
    # delay to every shutdown, which an orchestrator counts against its
    # termination grace period.
    backend = MemoryQueue()
    worker = Worker(backend, TaskRegistry(), poll_timeout=30.0)
    task = asyncio.create_task(worker.run())

    await asyncio.sleep(0.05)
    worker.stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert backend.closed


# -- registry -----------------------------------------------------------


def test_registering_the_same_task_twice_is_an_error() -> None:
    registry = TaskRegistry()
    registry.register("t", lambda payload: None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="already registered"):
        registry.register("t", lambda payload: None)  # type: ignore[arg-type]


def test_an_unknown_task_names_what_is_registered() -> None:
    registry = TaskRegistry()
    registry.register("send_email", lambda payload: None)  # type: ignore[arg-type]
    with pytest.raises(UnknownTask, match="send_email"):
        registry.get("send_emial")


async def test_the_worker_drains_in_flight_jobs_before_stopping() -> None:
    backend = MemoryQueue()
    registry = TaskRegistry()
    started = asyncio.Event()
    finished = asyncio.Event()

    @registry.task("slow")
    async def slow(payload: dict) -> None:
        started.set()
        await asyncio.sleep(0.2)
        finished.set()

    await backend.enqueue(Job(task="slow"))
    worker = Worker(backend, registry, poll_timeout=0.01)
    task = asyncio.create_task(worker.run())

    await asyncio.wait_for(started.wait(), timeout=2)
    worker.stop()
    await asyncio.wait_for(task, timeout=5)

    # Killing a job mid-write is how a queue produces half-applied effects.
    assert finished.is_set()
    assert len(backend.acked) == 1
    assert backend.closed


# -- the window a handler has to finish inside --------------------------


class _LeasedBackend:
    """A backend that publishes a visibility timeout, and nothing else."""

    visibility_timeout = 60

    async def setup(self) -> None: ...
    async def enqueue(self, job): ...  # type: ignore[no-untyped-def]
    async def dequeue(self, *, timeout: float = 5.0): ...  # type: ignore[no-untyped-def]
    async def ack(self, job) -> None: ...  # type: ignore[no-untyped-def]
    async def nack(self, job, *, retry: bool = True) -> None: ...  # type: ignore[no-untyped-def]
    async def stats(self): ...  # type: ignore[no-untyped-def]
    async def close(self) -> None: ...


class _UnleasedBackend(_LeasedBackend):
    """RabbitMQ's shape: redelivery is on the connection, not on a clock."""

    visibility_timeout = None  # type: ignore[assignment]


def test_a_handler_may_not_outlive_the_claim_that_protects_it() -> None:
    """The duplicate-execution bug, refused at construction.

    Nothing extends the lease while a handler runs, so a job that outlives the
    visibility timeout is claimed again by another worker while the first is
    still inside it. The queue then runs it twice -- a charge taken twice, an
    email sent twice -- and neither run knows about the other.
    """
    with pytest.raises(ValueError) as raised:
        Worker(_LeasedBackend(), TaskRegistry(), job_timeout=60)

    assert "visibility_timeout" in str(raised.value)
    assert "runs twice" in str(raised.value)


def test_the_default_leaves_room_for_the_nack_to_land() -> None:
    """Derived from the backend rather than defaulted beside it.

    Both numbers used to be 300 and they lived in different files, so the
    shipped default raced at the boundary and raising one without the other
    made the duplicate certain.
    """
    worker = Worker(_LeasedBackend(), TaskRegistry())

    assert worker.job_timeout == 48.0  # 60 * 0.8


def test_a_timeout_inside_the_window_is_taken_as_given() -> None:
    worker = Worker(_LeasedBackend(), TaskRegistry(), job_timeout=30)

    assert worker.job_timeout == 30


def test_a_backend_with_no_clock_has_nothing_to_check_against() -> None:
    """RabbitMQ holds the claim for as long as the connection lives, so there
    is no window to run inside and no arithmetic to do."""
    worker = Worker(_UnleasedBackend(), TaskRegistry(), job_timeout=3600)

    assert worker.job_timeout == 3600
