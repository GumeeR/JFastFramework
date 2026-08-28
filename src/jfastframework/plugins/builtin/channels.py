"""Channels as a plugin: declared once, backed by whatever each one needs.

    [plugins]
    enabled = ["observability", "cache", "channels"]

    [plugin.channels]
    module = "channels"        # where the declarations live
    default_backend = "memory" # memory | redis | kafka
    prefix = ""                # namespaces Redis channel names

Default-enabled with the in-process backend, so publishing an event needs no
infrastructure and no decision on day one. A channel that has to cross a
process, or a language, says so on itself::

    LARAVEL_CHEQUES = Channel("LARAVEL_CHEQUES_EVENTS", backend="redis")

which is the case this exists for: Laravel publishes, this service subscribes,
and the transport is Redis because that is what both speak.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from pydantic_settings import SettingsConfigDict

from jfastframework.channels import (
    Channel,
    ChannelBackend,
    ChannelError,
    ChannelRegistry,
    KafkaBackend,
    MemoryBackend,
    RedisBackend,
    pending_subscriptions,
)
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.context import AppContext


class ChannelsSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_CHANNELS_", env_file=".env", extra="ignore")

    # The module whose top-level Channel objects are the declarations. One
    # file, so the channels a system uses can be read in one place.
    module: str = "channels"
    default_backend: str = "memory"
    # Prefixes Redis channel names. Two environments sharing a Redis without
    # this is two environments delivering each other's events.
    prefix: str = ""


class ChannelsPlugin(Plugin):
    meta = PluginMeta(
        name="channels",
        version="0.1.0",
        description="Declared pub/sub channels over memory, Redis or Kafka.",
        after=("observability", "cache", "events"),
        provides=("channels",),
        default_enabled=False,
        # A channel backend being unreachable degrades the service.
        health_critical=False,
    )
    Settings = ChannelsSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._registry = ChannelRegistry()
        self._backends: dict[str, ChannelBackend] = {}

    # -- wiring --------------------------------------------------------

    def _backend_for(self, name: str, ctx: AppContext) -> ChannelBackend:
        if name in self._backends:
            return self._backends[name]

        settings: ChannelsSettings = self.settings
        backend: ChannelBackend
        if name == "memory":
            backend = MemoryBackend()
        elif name == "redis":
            client = ctx.optional("cache.client")
            if client is None:
                raise ChannelError(
                    "A channel asks for the redis backend, but the `cache` plugin "
                    'is not enabled. Add "cache" to [plugins].enabled.'
                )
            backend = RedisBackend(client, prefix=settings.prefix)
        elif name == "kafka":
            bus = ctx.optional("events")
            if bus is None:
                raise ChannelError(
                    "A channel asks for the kafka backend, but the `events` plugin "
                    'is not enabled. Add "events" to [plugins].enabled.'
                )
            backend = KafkaBackend(bus)
        else:
            raise ChannelError(f"Unknown channel backend {name!r}. Use memory, redis or kafka.")

        self._backends[name] = backend
        return backend

    def _discover(self) -> list[Channel]:
        """Every ``Channel`` declared at the top level of the module."""
        settings: ChannelsSettings = self.settings
        try:
            module = importlib.import_module(settings.module)
        except ModuleNotFoundError:
            # No declarations file is not an error: a service may register its
            # channels in code instead.
            return []
        return [value for value in vars(module).values() if isinstance(value, Channel)]

    def register(self, ctx: AppContext) -> None:
        settings: ChannelsSettings = self.settings

        for channel in self._discover():
            if channel.backend == "memory" and settings.default_backend != "memory":
                # A channel that did not choose gets the service's default.
                channel.backend = settings.default_backend
            self._registry.register(channel)

        for channel in self._registry.all():
            channel.bind(self._backend_for(channel.backend, ctx))

        ctx.provide("channels", self._registry)

    async def startup(self, ctx: AppContext) -> None:
        # Decorated handlers registered at import time are subscribed here:
        # importing a module must not require a running event loop.
        for channel, handler in pending_subscriptions():
            registered = self._registry.get(channel.name)
            if registered is None:
                self._registry.register(channel)
                channel.bind(self._backend_for(channel.backend, ctx))
                registered = channel
            await registered.subscribe(handler)
            ctx.logger.debug("subscribed %s to %s", handler.__name__, channel.name)

    async def shutdown(self, ctx: AppContext) -> None:
        for backend in self._backends.values():
            await backend.close()

    async def health(self, ctx: AppContext) -> HealthReport:
        backends = sorted(self._backends)
        return HealthReport.ok(
            f"{len(self._registry.all())} channel(s) over {', '.join(backends) or 'nothing'}",
            channels=[c.name for c in self._registry.all()],
            backends=backends,
        )

    def describe(self) -> dict[str, Any]:
        described = super().describe()
        described["channels"] = self._registry.describe()
        return described


__all__ = ["ChannelsPlugin", "ChannelsSettings"]
