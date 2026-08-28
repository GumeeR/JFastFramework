"""Redis cache, pub/sub and queue.

Publishes ``cache`` (a small typed facade) and ``cache.client`` (the raw redis
client, for anything the facade does not cover).

Convention inherited from the CometaX stack: DB 0 = cache, DB 1 = pub/sub,
DB 2 = queue. One Redis container, three logical namespaces.

Requires: ``pip install jfastframework[cache]``
"""

from __future__ import annotations

import json
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


class CacheSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_CACHE_", env_file=".env", extra="ignore")

    url: str = "redis://localhost:6379/0"
    default_ttl: int = 300
    key_prefix: str = ""

    include_infra: bool = True
    image: str = "redis:7-alpine"
    port_offset: int = 3


class Cache:
    """Namespaced JSON cache over a redis client."""

    def __init__(self, client: Any, *, prefix: str = "", default_ttl: int = 300) -> None:
        self._client = client
        self._prefix = prefix
        self._default_ttl = default_ttl

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}" if self._prefix else key

    async def get(self, key: str, default: Any = None) -> Any:
        raw = await self._client.get(self._key(key))
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        await self._client.set(
            self._key(key),
            json.dumps(value, default=str),
            ex=ttl if ttl is not None else self._default_ttl,
        )

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return int(await self._client.delete(*(self._key(k) for k in keys)))

    async def exists(self, key: str) -> bool:
        return bool(await self._client.exists(self._key(key)))

    async def publish(self, channel: str, message: Any) -> None:
        await self._client.publish(channel, json.dumps(message, default=str))


class CachePlugin(Plugin):
    meta = PluginMeta(
        name="cache",
        version="0.1.0",
        description="Redis cache, pub/sub and queue backend.",
        after=("observability",),
        provides=("cache", "cache.client"),
        default_enabled=False,
        extra="jfastframework[cache]",
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
            Cache(client, prefix=prefix, default_ttl=settings.default_ttl),
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
                healthcheck={
                    "test": ["CMD", "redis-cli", "ping"],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 10,
                },
            )
        ]
