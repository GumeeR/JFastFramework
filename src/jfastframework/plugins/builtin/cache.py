"""Redis cache, pub/sub and queue.

Publishes ``cache`` (a small typed facade) and ``cache.client`` (the raw redis
client, for anything the facade does not cover).

Convention inherited from the CometaX stack: DB 0 = cache, DB 1 = pub/sub,
DB 2 = queue. One Redis container, three logical namespaces.

``get_or_set`` is the resilient read path and the one to reach for: it absorbs
a backend failure by falling back to the loader, which is what makes
``health_critical=False`` an honest claim. The primitives below it --
``get``, ``set``, ``delete``, ``exists`` -- still raise, because a service that
cannot tell "cached nothing" from "Redis is gone" is a service that serves
stale answers forever without anyone noticing.

Requires: ``pip install jfastframework[cache]``
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic_settings import SettingsConfigDict

from jfastframework.plugins.base import (
    HealthReport,
    InfraService,
    Plugin,
    PluginMeta,
    PluginSettings,
)

if TYPE_CHECKING:
    from jfastframework.context import AppContext


# Distinguishes "no value stored" from a stored ``null``, which a caller is
# entitled to cache and which ``default=None`` would otherwise hide.
_MISS: Any = object()

# How often a caller waiting on the lock holder re-reads the key. Short enough
# that the wait is dominated by the loader rather than by the poll.
_POLL_INTERVAL = 0.02


class CacheSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_CACHE_", env_file=".env", extra="ignore")

    url: str = "redis://localhost:6379/0"
    default_ttl: int = 300
    key_prefix: str = ""

    # Upper bound on how long one caller may hold the recompute lock. It only
    # has to outlive a normal loader; a loader slower than this produces
    # duplicate work, never a wrong answer.
    stampede_lock_ttl: int = 10
    # How long a caller that lost the race waits for the winner's value before
    # loading for itself. Zero disables the lock entirely.
    stampede_wait: float = 2.0

    include_infra: bool = True
    image: str = "redis:7-alpine"
    port_offset: int = 3


class CacheMetrics:
    """Cache counters on the shared Prometheus registry.

    Constructed with ``registry=None`` when the ``metrics`` plugin is disabled,
    and every method is then a no-op. The cache never asks which case it is in.
    """

    def __init__(self, registry: Any = None) -> None:
        self._hits: Any = None
        self._misses: Any = None
        self._errors: Any = None
        self._suppressed: Any = None
        if registry is None:
            return

        from prometheus_client import Counter

        self._hits = Counter(
            "cache_hits_total", "Cache reads served from the cache.", registry=registry
        )
        self._misses = Counter(
            "cache_misses_total", "Cache reads that found nothing stored.", registry=registry
        )
        self._errors = Counter(
            "cache_errors_total", "Cache operations the backend refused.", registry=registry
        )
        self._suppressed = Counter(
            "cache_stampede_suppressed_total",
            "Loader runs skipped because another caller was already computing the key.",
            registry=registry,
        )

    @staticmethod
    def _inc(counter: Any) -> None:
        if counter is not None:
            counter.inc()

    def hit(self) -> None:
        self._inc(self._hits)

    def miss(self) -> None:
        self._inc(self._misses)

    def error(self) -> None:
        self._inc(self._errors)

    def suppressed(self) -> None:
        self._inc(self._suppressed)


class Cache:
    """Namespaced JSON cache over a redis client."""

    def __init__(
        self,
        client: Any,
        *,
        prefix: str = "",
        default_ttl: int = 300,
        stampede_lock_ttl: int = 10,
        stampede_wait: float = 2.0,
        metrics: CacheMetrics | None = None,
    ) -> None:
        self._client = client
        self._prefix = prefix
        self._default_ttl = default_ttl
        self._stampede_lock_ttl = stampede_lock_ttl
        self._stampede_wait = stampede_wait
        self._metrics = metrics if metrics is not None else CacheMetrics()

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}" if self._prefix else key

    def _lock_key(self, key: str) -> str:
        # Suffixed rather than prefixed so a lock cannot collide with a real
        # key: nothing legitimate ends in this.
        return f"{self._key(key)}:__jfast_lock"

    def _expiry(self, ttl: int | None) -> int | None:
        if ttl is None:
            return self._default_ttl
        if ttl < 0:
            raise ValueError(f"ttl must be >= 0, got {ttl}. Use ttl=0 to store without expiry.")
        # Redis has no "expire in zero seconds", so zero is free to mean the
        # one thing `ttl=None` cannot: store it until something deletes it.
        return ttl or None

    async def _read(self, key: str) -> Any:
        """Uncounted read. ``_MISS`` when nothing is stored."""
        raw = await self._client.get(self._key(key))
        if raw is None:
            return _MISS
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            # Another system writes to this Redis and does not owe us JSON.
            return raw

    async def get(self, key: str, default: Any = None) -> Any:
        try:
            found = await self._read(key)
        except Exception:
            self._metrics.error()
            raise
        if found is _MISS:
            self._metrics.miss()
            return default
        self._metrics.hit()
        return found

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        await self._client.set(
            self._key(key),
            json.dumps(value, default=str),
            ex=self._expiry(ttl),
        )

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return int(await self._client.delete(*(self._key(k) for k in keys)))

    async def exists(self, key: str) -> bool:
        return bool(await self._client.exists(self._key(key)))

    async def publish(self, channel: str, message: Any) -> None:
        # Deliberately unprefixed. A channel name is a contract with whoever
        # else is on this Redis -- often a Laravel app that never heard of our
        # key prefix. Namespacing channels is the `channels` plugin's job,
        # under its own setting.
        await self._client.publish(channel, json.dumps(message, default=str))

    # -- read-through --------------------------------------------------

    async def get_or_set(
        self,
        key: str,
        loader: Callable[[], Awaitable[Any]],
        *,
        ttl: int | None = None,
    ) -> Any:
        """Return the cached value, or run ``loader`` and cache what it gives.

        Every backend failure degrades to calling ``loader``: a cache that is
        down costs this call a round trip and a recomputation, never a 500.
        Errors raised by ``loader`` itself propagate -- masking those would
        turn a broken query into a quiet wrong answer.

        Concurrent misses on the same key are collapsed: one caller takes a
        short lock and recomputes while the others wait for its result, up to
        ``stampede_wait``. A caller that waits that long stops waiting and
        loads for itself, so a stalled loader costs duplicated work rather
        than a queue of stalled requests.
        """
        try:
            found = await self.get(key, _MISS)
        except Exception:  # noqa: BLE001
            found = _MISS
        if found is not _MISS:
            return found

        if self._stampede_wait <= 0:
            return await self._load(key, loader, ttl)

        if await self._acquire(key):
            try:
                return await self._load(key, loader, ttl)
            finally:
                await self._release(key)

        leader_value = await self._await_leader(key)
        if leader_value is not _MISS:
            self._metrics.suppressed()
            return leader_value
        return await self._load(key, loader, ttl)

    async def _load(
        self,
        key: str,
        loader: Callable[[], Awaitable[Any]],
        ttl: int | None,
    ) -> Any:
        value = await loader()
        try:
            await self.set(key, value, ttl)
        except Exception:  # noqa: BLE001
            # The caller asked for a value, not for it to be cached.
            self._metrics.error()
        return value

    async def _acquire(self, key: str) -> bool:
        """True if this caller may recompute.

        A backend that cannot answer grants the lock: the fallback for "we do
        not know" has to be doing the work, not refusing to.
        """
        try:
            taken = await self._client.set(
                self._lock_key(key),
                uuid.uuid4().hex,
                ex=self._stampede_lock_ttl,
                nx=True,
            )
        except Exception:  # noqa: BLE001
            self._metrics.error()
            return True
        return bool(taken)

    async def _release(self, key: str) -> None:
        # Unconditional: this is an optimisation, not mutual exclusion. If the
        # TTL already expired and another caller holds the lock, deleting it
        # costs one duplicate loader run and nothing else.
        try:
            await self._client.delete(self._lock_key(key))
        except Exception:  # noqa: BLE001
            self._metrics.error()

    async def _await_leader(self, key: str) -> Any:
        """Poll for the lock holder's value until ``stampede_wait`` runs out."""
        deadline = time.monotonic() + self._stampede_wait
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                found = await self._read(key)
            except Exception:  # noqa: BLE001
                self._metrics.error()
                return _MISS
            if found is not _MISS:
                return found
        return _MISS


class CachePlugin(Plugin):
    meta = PluginMeta(
        name="cache",
        version="0.1.0",
        description="Redis cache, pub/sub and queue backend.",
        # `metrics` is soft, not required: it publishes the registry the cache
        # counters live on, but a service that wants a cache should not have
        # to carry prometheus-client to get one.
        after=("observability", "metrics"),
        provides=("cache", "cache.client"),
        default_enabled=False,
        extra="jfastframework[cache]",
        # A cache that stops answering degrades the service. It does not
        # break it, and taking every replica out of rotation would.
        health_critical=False,
    )
    Settings = CacheSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._client: Any = None

    def register(self, ctx: AppContext) -> None:
        import redis.asyncio as aioredis

        settings: CacheSettings = self.settings
        prefix = settings.key_prefix or f"{ctx.settings.app_name}:"
        client = aioredis.from_url(settings.url, decode_responses=True)

        self._client = client
        ctx.provide("cache.client", client)
        ctx.provide(
            "cache",
            Cache(
                client,
                prefix=prefix,
                default_ttl=settings.default_ttl,
                stampede_lock_ttl=settings.stampede_lock_ttl,
                stampede_wait=settings.stampede_wait,
                # `after` puts metrics first when it is loaded; absent is the
                # normal case for a service that disabled it, not an error.
                metrics=CacheMetrics(ctx.get("metrics.registry")),
            ),
        )

    async def shutdown(self, ctx: AppContext) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._client is None:
            return HealthReport.fail("redis client not initialised")
        try:
            await self._client.ping()
        except Exception as exc:  # noqa: BLE001
            # Cache being down degrades the service; it does not break it.
            return HealthReport.fail(f"redis unreachable: {exc}", critical=False)
        return HealthReport.ok("redis reachable")

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        settings: CacheSettings = self.settings
        if not settings.include_infra:
            return []
        return [
            InfraService(
                name="redis",
                image=settings.image,
                port_offset=settings.port_offset,
                internal_port=6379,
                command="redis-server --appendonly yes",
                volumes=["redis_data:/data"],
                client_env={"JFAST_CACHE_URL": "redis://redis:6379/0"},
                healthcheck={
                    "test": ["CMD", "redis-cli", "ping"],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 10,
                },
            )
        ]
