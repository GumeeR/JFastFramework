"""Declared channels: one file that says what this system talks about.

The pattern this replaces is a module of string constants -- ``VINCULACION_
COMPLETADA = "eventos:vinculacion_completada"`` -- imported wherever somebody
publishes or subscribes. It works, and it fails in three specific ways:

* **Nothing checks the payload.** The publisher sends a dict, the subscriber
  reads a key that is no longer there, and the failure surfaces in a worker
  three services away from the change that caused it.
* **The transport is welded to the call site.** Reaching for ``redis.publish``
  directly means a channel cannot move to Kafka, or be tested without Redis
  running.
* **Nobody can list them.** The channels a system uses end up spread across the
  modules that happen to publish to them.

A ``Channel`` fixes the first two by carrying a payload type and going through
a backend, and the registry fixes the third: ``jfast describe`` can list every
channel a service declares.

Backends are per channel, not per service. A channel that Laravel also
publishes to has to be Redis because that is what Laravel speaks; an internal
one has no reason to need any infrastructure at all. Mixing them is the normal
case, not an edge case.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

logger = logging.getLogger("jfast.channels")

Payload = dict[str, Any]
Handler = Callable[[Payload], Awaitable[None]]
T = TypeVar("T")


class ChannelError(Exception):
    """A channel that cannot deliver, or a payload that does not belong on it."""


@runtime_checkable
class ChannelBackend(Protocol):
    """What a transport has to do. Three methods, on purpose."""

    async def publish(self, channel: str, payload: Payload) -> None: ...

    async def subscribe(self, channel: str, handler: Handler) -> None: ...

    async def close(self) -> None: ...


# -- backends -----------------------------------------------------------


class MemoryBackend:
    """In process. The default, and the reason a channel needs no infrastructure.

    Delivery is to handlers registered in *this* process, so it is correct for
    a modular monolith and wrong the moment there are two replicas. That is
    stated rather than hidden: ``jfast describe`` reports the backend, and
    moving to Redis is a line of configuration rather than a rewrite.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self.published: list[tuple[str, Payload]] = []

    async def publish(self, channel: str, payload: Payload) -> None:
        self.published.append((channel, payload))
        handlers = self._handlers.get(channel, [])
        if not handlers:
            return
        # gather, not a loop: one slow handler must not delay the others, and
        # one that raises must not stop them.
        results = await asyncio.gather(
            *(handler(payload) for handler in handlers), return_exceptions=True
        )
        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, BaseException):
                logger.exception(
                    "handler %s failed on %s",
                    getattr(handler, "__name__", handler),
                    channel,
                    exc_info=result,
                )

    async def subscribe(self, channel: str, handler: Handler) -> None:
        self._handlers.setdefault(channel, []).append(handler)

    async def close(self) -> None:
        self._handlers.clear()


class RedisBackend:
    """Redis pub/sub. Fan-out to every subscriber, and nothing is retained.

    The property to understand before choosing it: a subscriber that is not
    connected when a message is published never sees it. That is fine for
    "this changed, refresh" and wrong for "do this work" -- which is what the
    queue is for.
    """

    def __init__(self, client: Any, *, prefix: str = "") -> None:
        self._client = client
        self._prefix = prefix
        self._tasks: list[asyncio.Task[None]] = []

    def _name(self, channel: str) -> str:
        return f"{self._prefix}{channel}"

    async def publish(self, channel: str, payload: Payload) -> None:
        await self._client.publish(self._name(channel), json.dumps(payload, default=str))

    async def subscribe(self, channel: str, handler: Handler) -> None:
        pubsub = self._client.pubsub()
        await pubsub.subscribe(self._name(channel))

        async def listen() -> None:
            try:
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    raw = message["data"]
                    try:
                        payload = json.loads(raw)
                    except (TypeError, ValueError):
                        # A publisher in another language sent something that
                        # is not JSON. Log it and keep the listener alive.
                        logger.warning("unparseable message on %s: %r", channel, raw)
                        continue
                    try:
                        await handler(payload)
                    except Exception:
                        logger.exception("handler failed on %s", channel)
            except asyncio.CancelledError:
                raise
            finally:
                await pubsub.unsubscribe(self._name(channel))
                await pubsub.close()

        self._tasks.append(asyncio.create_task(listen(), name=f"channel:{channel}"))

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with_suppressed = asyncio.gather(task, return_exceptions=True)
            await with_suppressed
        self._tasks.clear()


class KafkaBackend:
    """Kafka, through the ``events`` plugin's bus.

    Retained and replayable, which is the reason to pick it over Redis: a
    consumer that was down catches up rather than missing what happened.
    """

    def __init__(self, bus: Any) -> None:
        self._bus = bus

    async def publish(self, channel: str, payload: Payload) -> None:
        await self._bus.publish(channel, payload)

    async def subscribe(self, channel: str, handler: Handler) -> None:
        await self._bus.subscribe(channel, handler)

    async def close(self) -> None:
        return None


# -- channels -----------------------------------------------------------


@dataclass
class Channel:
    """One named thing that happens, and what it carries.

    Declared once, imported by everyone who cares:

        VINCULACION_COMPLETADA = Channel(
            "eventos:vinculacion_completada",
            description="A linkage finished; balances downstream may be stale.",
            required=("vinculacion_id", "rfc"),
        )

    ``required`` is deliberately a set of key names rather than a model. These
    payloads cross language boundaries -- Laravel publishes to some of these --
    so the contract that can actually be enforced on both sides is "these keys
    are present", not "this is a pydantic model".
    """

    name: str
    description: str = ""
    # Backend name: memory, redis or kafka. Resolved by the plugin.
    backend: str = "memory"
    required: tuple[str, ...] = ()
    _backend: ChannelBackend | None = field(default=None, repr=False, compare=False)

    def bind(self, backend: ChannelBackend) -> None:
        self._backend = backend

    def _require_backend(self) -> ChannelBackend:
        if self._backend is None:
            raise ChannelError(
                f"Channel {self.name!r} has no backend. Enable the `channels` plugin, "
                f"or bind one explicitly in a test."
            )
        return self._backend

    def validate(self, payload: Payload) -> None:
        missing = [key for key in self.required if key not in payload]
        if missing:
            raise ChannelError(
                f"Payload for {self.name!r} is missing {', '.join(missing)}. "
                f"Required: {', '.join(self.required)}."
            )

    async def publish(self, payload: Payload) -> None:
        """Validate, then send. Validation is here so a bad payload fails in
        the service that built it, not in the subscriber that received it."""
        self.validate(payload)
        await self._require_backend().publish(self.name, payload)

    async def subscribe(self, handler: Handler) -> None:
        await self._require_backend().subscribe(self.name, handler)

    def on(self, handler: Handler) -> Handler:
        """Decorator form. Registration happens at startup, not at import."""
        _PENDING.append((self, handler))
        return handler

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "backend": self.backend,
            "required": list(self.required),
        }


# Handlers registered by decorator before the app exists. The plugin drains
# this at startup; importing a module must not need a running event loop.
_PENDING: list[tuple[Channel, Handler]] = []


def pending_subscriptions() -> list[tuple[Channel, Handler]]:
    return list(_PENDING)


def clear_pending() -> None:
    """For tests. Decorators accumulate across a session otherwise."""
    _PENDING.clear()


class ChannelRegistry:
    """Every channel a service declares, so something can list them."""

    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}

    def register(self, channel: Channel) -> Channel:
        existing = self._channels.get(channel.name)
        if existing is not None and existing is not channel:
            raise ChannelError(
                f"Two channels are both called {channel.name!r}. A channel name is "
                f"a wire contract; two of them is a message going somewhere "
                f"unintended."
            )
        self._channels[channel.name] = channel
        return channel

    def get(self, name: str) -> Channel | None:
        return self._channels.get(name)

    def all(self) -> tuple[Channel, ...]:
        return tuple(self._channels.values())

    def describe(self) -> list[dict[str, Any]]:
        return [c.describe() for c in sorted(self._channels.values(), key=lambda c: c.name)]


__all__ = [
    "Channel",
    "ChannelBackend",
    "ChannelError",
    "ChannelRegistry",
    "Handler",
    "KafkaBackend",
    "MemoryBackend",
    "Payload",
    "RedisBackend",
    "clear_pending",
    "pending_subscriptions",
]
