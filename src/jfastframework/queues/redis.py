"""Job queue on Redis.

Faster than the PostgreSQL queue and with a real blocking wait, so a worker
picks up a job in microseconds instead of on the next poll.

The reliability comes from ``BLMOVE``: a claimed job is moved atomically to a
per-worker processing list, so a worker that dies leaves the job visible for
recovery instead of losing it. A naive ``BRPOP`` queue drops that job on the
floor, which is why this one is more code than you might expect.

Trade-off against PostgreSQL: enqueueing cannot share the transaction that
produced the work. Commit the row, crash before the ``LPUSH``, and the job
never exists. Where that matters — money, state machines — keep the queue in
the database.

Requires: ``pip install jfastframework[cache]``
"""

from __future__ import annotations

import contextlib
import socket
from typing import Any

from jfastframework.queues.base import Job


class RedisQueue:
    def __init__(
        self,
        client: Any,
        *,
        name: str = "jfast:jobs",
        visibility_timeout: int = 300,
        consumer: str | None = None,
    ) -> None:
        self._client = client
        self._pending = f"{name}:pending"
        self._delayed = f"{name}:delayed"
        self._dead = f"{name}:dead"
        # One processing list per worker, so recovery can tell whose job it is.
        self._consumer = consumer or f"{socket.gethostname()}:{id(self)}"
        self._processing = f"{name}:processing:{self._consumer}"
        self._visibility = visibility_timeout

    async def setup(self) -> None:
        # Redis needs no schema. Recover anything this consumer name left
        # behind on a previous run before taking new work.
        await self._recover_own()

    async def _recover_own(self) -> int:
        recovered = 0
        while await self._client.rpoplpush(self._processing, self._pending):
            recovered += 1
        return recovered

    async def _promote_due(self) -> None:
        """Move delayed jobs whose time has come into the pending list."""
        import time

        now = time.time()
        due = await self._client.zrangebyscore(self._delayed, 0, now, start=0, num=100)
        for raw in due:
            # Only the client that wins the ZREM enqueues it, so a job cannot
            # be promoted twice by two workers racing.
            if await self._client.zrem(self._delayed, raw):
                await self._client.lpush(self._pending, raw)

    async def enqueue(self, job: Job) -> str:
        raw = job.to_json()
        if job.available_at is not None:
            await self._client.zadd(self._delayed, {raw: job.available_at.timestamp()})
        else:
            await self._client.lpush(self._pending, raw)
        return job.id

    async def dequeue(self, *, timeout: float = 5.0) -> Job | None:
        await self._promote_due()
        raw = await self._client.blmove(
            self._pending, self._processing, timeout=timeout, src="RIGHT", dest="LEFT"
        )
        if raw is None:
            return None

        job = Job.from_json(raw, receipt=raw)
        job.attempts += 1
        return job

    async def ack(self, job: Job) -> None:
        # Remove this exact payload from the processing list. LREM by value is
        # correct here because the receipt is the payload we moved.
        await self._client.lrem(self._processing, 1, job.receipt)

    async def nack(self, job: Job, *, retry: bool = True) -> None:
        import time

        await self._client.lrem(self._processing, 1, job.receipt)

        if not retry or job.exhausted:
            await self._client.lpush(self._dead, job.to_json())
            return

        available_at = time.time() + job.backoff().total_seconds()
        await self._client.zadd(self._delayed, {job.to_json(): available_at})

    async def stats(self) -> dict[str, int]:
        return {
            "pending": int(await self._client.llen(self._pending)),
            "delayed": int(await self._client.zcard(self._delayed)),
            "running": int(await self._client.llen(self._processing)),
            "dead": int(await self._client.llen(self._dead)),
        }

    async def health(self) -> tuple[bool, str]:
        try:
            await self._client.ping()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return False, f"redis queue unreachable: {exc}"
        return True, f"redis queue {self._pending} reachable"

    async def close(self) -> None:
        # Return unfinished work before shutting down, so a rolling deploy
        # does not park jobs in a processing list nobody will read again.
        with contextlib.suppress(Exception):
            await self._recover_own()

    def __repr__(self) -> str:
        return f"<RedisQueue name={self._pending!r} consumer={self._consumer!r}>"
