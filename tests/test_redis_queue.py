"""The Redis queue, against an in-memory double of the commands it uses.

The bug this file exists for: recovery used to be per-consumer under a name
that included ``id(self)``, a memory address. A worker that died came back
under a different name and never recovered its own in-flight jobs, so the
visibility timeout that ``queues.base`` documents as a guarantee did not exist
here at all.

A real broker still belongs in CI -- these tests prove the algorithm, not the
client library.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from jfastframework.queues.base import Job
from jfastframework.queues.redis import DEAD_AFTER, RedisQueue, default_consumer


class FakeRedis:
    """Just the commands RedisQueue issues, with a clock the test controls."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.now = 1_700_000_000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def time(self) -> tuple[int, int]:
        return int(self.now), int((self.now % 1) * 1_000_000)

    async def ping(self) -> bool:
        return True

    # -- lists ---------------------------------------------------------

    async def lpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def rpoplpush(self, src: str, dst: str) -> str | None:
        source = self.lists.get(src)
        if not source:
            return None
        value = source.pop()
        self.lists.setdefault(dst, []).insert(0, value)
        return value

    async def blmove(
        self,
        first_list: str,
        second_list: str,
        *,
        timeout: float,
        src: str = "LEFT",
        dest: str = "RIGHT",
    ) -> str | None:
        # The queue always moves RIGHT -> LEFT, which is what rpoplpush does.
        assert (src, dest) == ("RIGHT", "LEFT"), "unexpected direction"
        return await self.rpoplpush(first_list, second_list)

    async def lrem(self, key: str, count: int, value: str) -> int:
        target = self.lists.get(key, [])
        if value not in target:
            return 0
        target.remove(value)
        return 1

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    # -- hashes --------------------------------------------------------

    async def hset(self, key: str, field: str, value: Any) -> int:
        self.hashes.setdefault(key, {})[field] = str(value)
        return 1

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key: str, field: str) -> int:
        return 1 if self.hashes.get(key, {}).pop(field, None) is not None else 0

    # -- sorted sets ---------------------------------------------------

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zrangebyscore(
        self, key: str, low: float, high: float, *, start: int = 0, num: int = 100
    ) -> list[str]:
        members = sorted(
            (m for m, score in self.zsets.get(key, {}).items() if low <= score <= high),
            key=lambda m: self.zsets[key][m],
        )
        return members[start : start + num]

    async def zrem(self, key: str, member: str) -> int:
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))


def _queue(client: FakeRedis, consumer: str, *, visibility: int = 60) -> RedisQueue:
    return RedisQueue(client, visibility_timeout=visibility, consumer=consumer)


# -- the regression ----------------------------------------------------


def test_consumer_name_is_stable_within_a_process() -> None:
    """Two instances must agree on a name, or neither can recover the other."""
    assert default_consumer() == default_consumer()


def test_worker_id_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JFAST_WORKER_ID", "billing-worker-2")
    assert default_consumer() == "billing-worker-2"


def test_two_instances_share_a_processing_list() -> None:
    """The old key embedded id(self), so a restart orphaned its own jobs."""
    client = FakeRedis()
    first = RedisQueue(client, consumer="worker-a")
    second = RedisQueue(client, consumer="worker-a")
    assert first._processing == second._processing


async def test_a_dead_worker_hands_its_job_back() -> None:
    client = FakeRedis()
    alive = _queue(client, "worker-a")
    await alive.setup()
    await alive.enqueue(Job(task="send_invoice"))

    claimed = await alive.dequeue(timeout=0)
    assert claimed is not None
    assert await client.llen(alive._processing) == 1

    # worker-a dies here: no ack, and no more heartbeats.
    client.advance(60 * DEAD_AFTER + 1)

    successor = _queue(client, "worker-b")
    await successor.setup()
    recovered = await successor.dequeue(timeout=0)

    assert recovered is not None
    assert recovered.task == "send_invoice"
    assert await client.llen(alive._processing) == 0
    assert client.hashes["jfast:jobs:workers"].get("worker-a") is None


async def test_a_live_worker_keeps_its_job() -> None:
    """The other half: reaping a worker that is merely slow duplicates work."""
    client = FakeRedis()
    busy = _queue(client, "worker-a")
    await busy.setup()
    await busy.enqueue(Job(task="slow"))
    assert await busy.dequeue(timeout=0) is not None

    client.advance(10)  # well inside the visibility timeout
    await busy._heartbeat()

    peer = _queue(client, "worker-b")
    await peer.setup()
    assert await peer.dequeue(timeout=0) is None
    assert await client.llen(busy._processing) == 1


async def test_ack_clears_the_processing_list() -> None:
    client = FakeRedis()
    queue = _queue(client, "worker-a")
    await queue.setup()
    await queue.enqueue(Job(task="t"))
    job = await queue.dequeue(timeout=0)
    assert job is not None
    await queue.ack(job)
    assert await client.llen(queue._processing) == 0


async def test_close_returns_work_immediately() -> None:
    """A rolling deploy should not park jobs until the timeout expires."""
    client = FakeRedis()
    queue = _queue(client, "worker-a")
    await queue.setup()
    await queue.enqueue(Job(task="t"))
    assert await queue.dequeue(timeout=0) is not None

    await queue.close()

    assert await client.llen(queue._processing) == 0
    assert await client.llen("jfast:jobs:pending") == 1
    assert "worker-a" not in client.hashes["jfast:jobs:workers"]


# -- the rest of the protocol ------------------------------------------


async def test_a_delayed_job_waits_then_runs() -> None:
    client = FakeRedis()
    queue = _queue(client, "worker-a")
    await queue.setup()

    from datetime import UTC, datetime

    due = datetime.fromtimestamp(client.now + 30, tz=UTC)
    await queue.enqueue(Job(task="later", available_at=due))

    assert await queue.dequeue(timeout=0) is None
    client.advance(31)
    job = await queue.dequeue(timeout=0)
    assert job is not None
    assert job.task == "later"


async def test_retry_goes_back_with_backoff_and_exhaustion_is_dead() -> None:
    client = FakeRedis()
    queue = _queue(client, "worker-a")
    await queue.setup()
    await queue.enqueue(Job(task="flaky", max_attempts=2))

    first = await queue.dequeue(timeout=0)
    assert first is not None
    await queue.nack(first)
    assert await client.zcard("jfast:jobs:delayed") == 1

    client.advance(3600)
    second = await queue.dequeue(timeout=0)
    assert second is not None
    assert second.attempts == 2
    await queue.nack(second)

    assert await client.llen("jfast:jobs:dead") == 1


async def test_stats_count_every_worker() -> None:
    client = FakeRedis()
    first = _queue(client, "worker-a")
    second = _queue(client, "worker-b")
    await first.setup()
    await second.setup()

    await first.enqueue(Job(task="a"))
    await first.enqueue(Job(task="b"))
    assert await first.dequeue(timeout=0) is not None
    assert await second.dequeue(timeout=0) is not None

    stats = await first.stats()
    assert stats["running"] == 2
    assert stats["workers"] == 2
    assert stats["pending"] == 0


async def test_health_reports_the_queue_name() -> None:
    queue = _queue(FakeRedis(), "worker-a")
    healthy, detail = await queue.health()
    assert healthy
    assert "jfast:jobs:pending" in detail


def test_default_consumer_has_no_memory_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JFAST_WORKER_ID", raising=False)
    name = default_consumer()
    assert name.endswith(f":{os.getpid()}")
