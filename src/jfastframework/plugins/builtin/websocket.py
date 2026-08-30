"""WebSockets: connections addressed by *who*, not by socket.

    [plugins]
    enabled = ["observability", "cache", "auth", "websocket"]

    [plugin.websocket]
    channel = "websocket.fanout"
    send_buffer = 32
    heartbeat_seconds = 20
    idle_timeout_seconds = 60

Declaring an endpoint::

    from jfastframework.plugins.builtin.websocket import Connection, socket

    @socket("/ws/notifications", scopes=("notifications:read",))
    async def notifications(conn: Connection) -> None:
        await conn.join("orders")
        await conn.listen()

and sending to a person, from anywhere, on any worker::

    hub = ctx.require("websockets")
    await hub.send("user-42", {"kind": "invoice.paid", "id": 7})

The hard part -- getting a message to whichever worker holds that person's
socket -- is ``jfastframework.channels``, which is already written. What this
plugin adds is the ~200 lines every project rewrites around it: a registry
that maps a subject to its live sockets and forgets them when they die, a
handshake that authenticates before the upgrade, a bounded send buffer with a
stated policy, and a heartbeat.

**Every worker subscribes to one channel and delivers only to the sockets it
holds.** A publish never delivers locally; local delivery happens only through
the subscription. That is what makes it exactly once per socket: doing both
would deliver twice on the publishing worker, and the load balancer needs no
sticky sessions because no worker is special.

The cost of one channel is that every worker sees every message. Per-subject
Redis channels would cut that, at the price of a SUBSCRIBE and UNSUBSCRIBE per
connection; that trade is worth revisiting above a few thousand messages a
second, and not before.

**What this does not promise.** Redis pub/sub retains nothing, and nothing here
buffers per client. A message published while a client is between sockets is
lost -- there is no replay, no ack, and no ordering guarantee across a
reconnect. Clients must re-read their state when they reconnect. Anything that
must not be lost belongs in the queue, not here.

Requires: ``pip install jfastframework[server,cache,auth]``
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect
from pydantic_settings import SettingsConfigDict

from jfastframework.auth.principal import Principal
from jfastframework.channels import Channel, Payload, RedisBackend
from jfastframework.errors import PluginError
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.context import AppContext

logger = logging.getLogger("jfast.websocket")

SocketHandler = Callable[["Connection"], Awaitable[None]]
TokenVerifier = Callable[[str], Awaitable[Principal]]

# A browser cannot set an Authorization header on a WebSocket, but it can offer
# subprotocols -- so the token travels in a header instead of the URL. The
# server must echo this value back or the browser aborts the connection.
AUTH_SUBPROTOCOL = "jfast.auth.bearer"

# RFC 6455 close codes used here. 1013 is "try again later": the honest thing
# to tell a client whose buffer we refused to keep growing.
CLOSE_UNAUTHORIZED = 1008
CLOSE_GOING_AWAY = 1001
CLOSE_TRY_LATER = 1013
CLOSE_INTERNAL = 1011

OVERFLOW_POLICIES = ("close", "drop_oldest")


# -- the envelope -------------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    """One addressed message, as it crosses the backplane.

    The address travels with the payload rather than in the channel name: one
    channel means one subscription per worker, and a worker that gains a socket
    does not have to subscribe to anything.
    """

    kind: str
    target: str
    tenant_id: str | None
    data: Payload

    def to_payload(self) -> Payload:
        return {
            "kind": self.kind,
            "target": self.target,
            "tenant_id": self.tenant_id,
            "data": self.data,
        }

    @classmethod
    def from_payload(cls, payload: Payload) -> Envelope:
        return cls(
            kind=str(payload.get("kind", "subject")),
            target=str(payload.get("target", "")),
            tenant_id=payload.get("tenant_id"),
            data=payload.get("data") or {},
        )


# -- one connection -----------------------------------------------------


class Connection:
    """One live socket, and everything the hub needs to reach it.

    Sends go through a bounded queue drained by a writer task, never straight
    to ``send_text``: without that, a publisher awaits a TCP write to a slow
    client, and one stalled reader blocks the fan-out for everyone else.
    """

    def __init__(
        self,
        socket: WebSocket,
        principal: Principal,
        hub: WebSocketHub,
        *,
        buffer: int,
        overflow: str,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.socket = socket
        self.principal = principal
        self.hub = hub
        self.rooms: set[str] = set()
        self.dropped = 0
        self.received = 0
        self.last_seen = asyncio.get_running_loop().time()
        self._outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=buffer)
        self._overflow = overflow
        self._finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._closing: tuple[int, str] | None = None
        self._receiving: asyncio.Task[str] | None = None

    @property
    def subject(self) -> str:
        return self.principal.subject

    @property
    def tenant_id(self) -> str | None:
        return self.principal.tenant_id

    @property
    def key(self) -> tuple[str | None, str]:
        return (self.tenant_id, self.subject)

    def room_key(self, room: str) -> tuple[str | None, str]:
        # The tenant is taken from the verified token, never from the client:
        # that is what stops one tenant joining another's room by name.
        return (self.tenant_id, room)

    # -- sending -------------------------------------------------------

    def enqueue(self, frame: str) -> bool:
        """Queue a frame. False means the buffer is full under the close policy."""
        try:
            self._outbox.put_nowait(frame)
        except asyncio.QueueFull:
            if self._overflow == "close":
                return False
            self.dropped += 1
            with contextlib.suppress(asyncio.QueueEmpty):
                self._outbox.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._outbox.put_nowait(frame)
        return True

    async def send(self, data: Payload, *, room: str | None = None) -> bool:
        """Push straight to this one socket, bypassing the backplane."""
        return self.enqueue(json.dumps({"type": "message", "room": room, "data": data}))

    async def _pump(self) -> None:
        while True:
            frame = await self._outbox.get()
            try:
                await self.socket.send_text(frame)
            except Exception as exc:  # noqa: BLE001
                # A socket that died between frames. Nothing to tell the peer;
                # the point is to stop holding a registry entry for it.
                logger.debug("send failed on %s: %s", self.id, exc)
                self.fail(CLOSE_INTERNAL, "send failed")
                return

    # -- receiving -----------------------------------------------------

    async def receive(self) -> Payload | None:
        """The next application message, or None once the connection is over.

        Pongs are absorbed here: they are transport bookkeeping, and a handler
        that has to filter them is a handler that will forget to.
        """
        while True:
            if self._finished.done():
                return None
            if self._receiving is None:
                self._receiving = asyncio.create_task(self.socket.receive_text())

            waiters: set[asyncio.Future[Any]] = {self._receiving, self._finished}
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if self._receiving not in done:
                # The hub is closing this connection. Abandon the read; nothing
                # else will await it.
                self._receiving.cancel()
                self._receiving = None
                return None

            task, self._receiving = self._receiving, None
            try:
                raw = task.result()
            except WebSocketDisconnect:
                self.fail(CLOSE_GOING_AWAY, "client disconnected")
                return None
            except Exception as exc:  # noqa: BLE001
                logger.debug("receive failed on %s: %s", self.id, exc)
                self.fail(CLOSE_INTERNAL, "receive failed")
                return None

            self.last_seen = asyncio.get_running_loop().time()
            try:
                message = json.loads(raw)
            except ValueError:
                logger.info("non-JSON frame on %s", self.id)
                continue
            if not isinstance(message, dict):
                logger.info("non-object frame on %s", self.id)
                continue
            if message.get("type") in {"ping", "pong"}:
                continue
            self.received += 1
            return message

    def __aiter__(self) -> Connection:
        return self

    async def __anext__(self) -> Payload:
        message = await self.receive()
        if message is None:
            raise StopAsyncIteration
        return message

    async def listen(self) -> None:
        """Hold the socket open, ignoring whatever the client sends.

        The whole body of a push-only endpoint. A handler that returns closes
        the socket, which is right for a handler that finished and wrong for
        one that only ever wanted to receive.
        """
        while await self.receive() is not None:
            pass

    # -- rooms ---------------------------------------------------------

    async def join(self, room: str) -> None:
        self.hub.registry.join(self, room)

    async def leave(self, room: str) -> None:
        self.hub.registry.leave(self, room)

    # -- teardown ------------------------------------------------------

    def fail(self, code: int, reason: str) -> None:
        """Mark this connection finished. Idempotent; the first reason wins."""
        if self._finished.done():
            return
        self._closing = (code, reason)
        self._finished.set_result(None)

    @property
    def closing(self) -> tuple[int, str] | None:
        return self._closing


# -- the registry -------------------------------------------------------


class ConnectionRegistry:
    """Which sockets belong to whom, on this worker only.

    Keyed by ``(tenant_id, subject)`` rather than by subject: two tenants can
    name a user the same thing, and a registry that ignores the tenant is a
    cross-tenant delivery waiting for the collision.
    """

    def __init__(self) -> None:
        self._by_subject: dict[tuple[str | None, str], set[Connection]] = {}
        self._by_room: dict[tuple[str | None, str], set[Connection]] = {}

    def add(self, conn: Connection) -> None:
        self._by_subject.setdefault(conn.key, set()).add(conn)

    def discard(self, conn: Connection) -> None:
        """Remove every trace of a connection. Called from a ``finally``.

        Rooms are cleared here too: a connection that dies inside a room and is
        only removed from the subject index leaves the room holding it forever,
        which is the leak that looks like a slow memory leak in production.
        """
        holders = self._by_subject.get(conn.key)
        if holders is not None:
            holders.discard(conn)
            if not holders:
                del self._by_subject[conn.key]
        for room in list(conn.rooms):
            self.leave(conn, room)

    def join(self, conn: Connection, room: str) -> None:
        conn.rooms.add(room)
        self._by_room.setdefault(conn.room_key(room), set()).add(conn)

    def leave(self, conn: Connection, room: str) -> None:
        conn.rooms.discard(room)
        key = conn.room_key(room)
        members = self._by_room.get(key)
        if members is None:
            return
        members.discard(conn)
        if not members:
            del self._by_room[key]

    def for_subject(self, subject: str, *, tenant_id: str | None = None) -> tuple[Connection, ...]:
        return tuple(self._by_subject.get((tenant_id, subject), ()))

    def for_room(self, room: str, *, tenant_id: str | None = None) -> tuple[Connection, ...]:
        return tuple(self._by_room.get((tenant_id, room), ()))

    def for_tenant(self, tenant_id: str | None) -> tuple[Connection, ...]:
        found: list[Connection] = []
        for (tenant, _), holders in self._by_subject.items():
            if tenant == tenant_id:
                found.extend(holders)
        return tuple(found)

    def recipients(self, envelope: Envelope) -> tuple[Connection, ...]:
        if envelope.kind == "room":
            return self.for_room(envelope.target, tenant_id=envelope.tenant_id)
        if envelope.kind == "tenant":
            return self.for_tenant(envelope.tenant_id)
        return self.for_subject(envelope.target, tenant_id=envelope.tenant_id)

    def all(self) -> tuple[Connection, ...]:
        found: list[Connection] = []
        for holders in self._by_subject.values():
            found.extend(holders)
        return tuple(found)

    @property
    def total(self) -> int:
        return sum(len(holders) for holders in self._by_subject.values())

    @property
    def rooms(self) -> tuple[tuple[str | None, str], ...]:
        return tuple(sorted(self._by_room, key=lambda key: (key[0] or "", key[1])))

    def describe(self) -> dict[str, Any]:
        return {
            "connections": self.total,
            "subjects": len(self._by_subject),
            "rooms": len(self._by_room),
        }


# -- the hub ------------------------------------------------------------


class WebSocketHub:
    """The thing a service talks to: address a person, get a frame delivered."""

    def __init__(
        self,
        channel: Channel,
        *,
        verify: TokenVerifier,
        send_buffer: int = 32,
        overflow: str = "close",
        heartbeat_seconds: float = 20.0,
        idle_timeout_seconds: float = 60.0,
        close_on_token_expiry: bool = True,
        allow_first_message_auth: bool = False,
        auth_deadline_seconds: float = 5.0,
    ) -> None:
        if overflow not in OVERFLOW_POLICIES:
            raise PluginError(
                f"websocket overflow must be one of {', '.join(OVERFLOW_POLICIES)}, "
                f"not {overflow!r}."
            )
        self._channel = channel
        self._verify = verify
        self._buffer = send_buffer
        self._overflow = overflow
        self._heartbeat = heartbeat_seconds
        self._idle_timeout = idle_timeout_seconds
        self._close_on_expiry = close_on_token_expiry
        self._first_message = allow_first_message_auth
        self._auth_deadline = auth_deadline_seconds
        self.registry = ConnectionRegistry()

    async def start(self) -> None:
        await self._channel.subscribe(self._deliver)

    # -- sending -------------------------------------------------------

    async def send(self, subject: str, data: Payload, *, tenant_id: str | None = None) -> None:
        """Deliver to every socket that subject has, wherever it is connected.

        In a multi-tenant service the tenant is part of the address. A message
        sent without one reaches only connections whose token carried none.
        """
        await self._publish(Envelope("subject", subject, tenant_id, data))

    async def send_to_room(self, room: str, data: Payload, *, tenant_id: str | None = None) -> None:
        await self._publish(Envelope("room", room, tenant_id, data))

    async def broadcast(self, data: Payload, *, tenant_id: str | None = None) -> None:
        await self._publish(Envelope("tenant", "", tenant_id, data))

    async def _publish(self, envelope: Envelope) -> None:
        # Always through the channel, never straight into the local registry.
        # Delivering both ways would give every socket on the publishing worker
        # two copies of the same message.
        await self._channel.publish(envelope.to_payload())

    async def _deliver(self, payload: Payload) -> None:
        envelope = Envelope.from_payload(payload)
        frame = json.dumps(
            {
                "type": "message",
                "room": envelope.target if envelope.kind == "room" else None,
                "data": envelope.data,
            }
        )
        for conn in self.registry.recipients(envelope):
            if not conn.enqueue(frame):
                logger.info(
                    "closing a socket that stopped reading",
                    extra={"connection": conn.id, "subject": conn.subject},
                )
                conn.fail(CLOSE_TRY_LATER, "send buffer full")

    # -- serving one socket --------------------------------------------

    async def serve(
        self,
        socket: WebSocket,
        handler: SocketHandler,
        *,
        scopes: tuple[str, ...] = (),
    ) -> None:
        """Authenticate, register, run the handler, and always clean up."""
        principal = await self._handshake(socket, scopes)
        if principal is None:
            return

        conn = Connection(socket, principal, self, buffer=self._buffer, overflow=self._overflow)
        self.registry.add(conn)
        pump = asyncio.create_task(conn._pump(), name=f"ws-pump:{conn.id}")
        beat = asyncio.create_task(self._heartbeats(conn), name=f"ws-beat:{conn.id}")
        try:
            await handler(conn)
        except WebSocketDisconnect:
            pass
        finally:
            conn.fail(CLOSE_GOING_AWAY, "handler finished")
            for task in (pump, beat):
                task.cancel()
            # Both are being torn down; whatever they raise on the way out must
            # not stop the registry entry from going.
            with contextlib.suppress(BaseException):
                await asyncio.gather(pump, beat, return_exceptions=True)
            self.registry.discard(conn)
            code, reason = conn.closing or (CLOSE_GOING_AWAY, "")
            with contextlib.suppress(Exception):
                await socket.close(code=code, reason=reason)

    async def _handshake(self, socket: WebSocket, scopes: tuple[str, ...]) -> Principal | None:
        """Verify before the upgrade, and never read the query string.

        A token in the URL is written to every access log, proxy trace and
        browser history entry it passes through, and cannot be rotated out of
        any of them. The two places left are the Authorization header, which no
        browser can set on a WebSocket, and the subprotocol list, which every
        browser can -- so both are read, in that order.
        """
        token, subprotocol = _token_from(socket)
        accepted = False

        if token is None:
            if not self._first_message:
                # Closing before accept() answers the HTTP handshake with a
                # rejection: no socket is ever upgraded.
                await socket.close(code=CLOSE_UNAUTHORIZED, reason="authentication required")
                return None
            await socket.accept()
            accepted = True
            token = await self._first_message_token(socket)
            if token is None:
                return None

        try:
            principal = await self._verify(token)
        except Exception as exc:  # noqa: BLE001
            # The reason belongs in the log, never in the close frame.
            logger.info("websocket token rejected", extra={"reason": str(exc)})
            await socket.close(code=CLOSE_UNAUTHORIZED, reason="invalid token")
            return None

        if principal.claims.get("typ") == "refresh":
            # Same key, same issuer, same audience: without this check a 30-day
            # refresh token opens a socket wherever an access token would.
            logger.info("refresh token used to open a socket", extra={"subject": principal.subject})
            await socket.close(code=CLOSE_UNAUTHORIZED, reason="invalid token")
            return None

        if scopes and not principal.has_scope(*scopes):
            await socket.close(code=CLOSE_UNAUTHORIZED, reason="missing scope")
            return None

        if not accepted:
            await socket.accept(subprotocol=subprotocol)
        return principal

    async def _first_message_token(self, socket: WebSocket) -> str | None:
        """Opt-in fallback for a client that controls neither header.

        Off by default, because it inverts the order: the socket exists, and
        holds a file descriptor, before anybody has proved who they are. The
        deadline is what bounds that window.
        """
        try:
            raw = await asyncio.wait_for(socket.receive_text(), timeout=self._auth_deadline)
            message = json.loads(raw)
        except (TimeoutError, ValueError, WebSocketDisconnect):
            await socket.close(code=CLOSE_UNAUTHORIZED, reason="authentication required")
            return None
        token = message.get("token") if isinstance(message, dict) else None
        if not isinstance(token, str) or not token:
            await socket.close(code=CLOSE_UNAUTHORIZED, reason="authentication required")
            return None
        return token

    async def _heartbeats(self, conn: Connection) -> None:
        """Ping, and decide when silence means the peer is gone.

        A dropped TCP connection is not observable from the ASGI layer: the
        socket looks writable for as long as the kernel buffer accepts. Only
        something arriving proves the peer is alive, which is why the timeout
        is on inbound traffic and not on sends.
        """
        ping = json.dumps({"type": "ping"})
        while True:
            await asyncio.sleep(self._heartbeat)
            now = asyncio.get_running_loop().time()

            if self._close_on_expiry and conn.principal.is_expired:
                # A socket opened with a 15-minute token must not still be open
                # in eight hours: nothing else would ever re-check the grant,
                # and a logout could not reach it.
                conn.fail(CLOSE_UNAUTHORIZED, "token expired")
                return
            if self._idle_timeout and (now - conn.last_seen) > self._idle_timeout:
                conn.fail(CLOSE_GOING_AWAY, "no traffic")
                return
            if not conn.enqueue(ping):
                conn.fail(CLOSE_TRY_LATER, "send buffer full")
                return

    async def close_all(self, code: int = CLOSE_GOING_AWAY, reason: str = "shutting down") -> None:
        for conn in self.registry.all():
            conn.fail(code, reason)


# -- declaration --------------------------------------------------------


@dataclass(frozen=True)
class SocketRoute:
    path: str
    handler: SocketHandler
    scopes: tuple[str, ...] = ()
    name: str = ""


# Endpoints declared by decorator before the app exists. The plugin drains this
# when it registers; importing a module must not require a running app.
_PENDING: list[SocketRoute] = []


def socket(
    path: str, *, scopes: tuple[str, ...] = (), name: str = ""
) -> Callable[[SocketHandler], SocketHandler]:
    """Declare a WebSocket endpoint at import time::

        @socket("/ws/notifications", scopes=("notifications:read",))
        async def notifications(conn: Connection) -> None:
            await conn.listen()

    The handler runs once per connection and owns it: returning closes the
    socket. Everything before it -- the handshake, the registry entry, the send
    buffer, the heartbeat -- is the plugin's.
    """

    def decorator(handler: SocketHandler) -> SocketHandler:
        _PENDING.append(SocketRoute(path, handler, tuple(scopes), name or handler.__name__))
        return handler

    return decorator


def pending_sockets() -> list[SocketRoute]:
    return list(_PENDING)


def clear_pending() -> None:
    """For tests. Decorators accumulate across a session otherwise."""
    _PENDING.clear()


# -- the plugin ---------------------------------------------------------


class WebSocketSettings(PluginSettings):
    model_config = SettingsConfigDict(
        env_prefix="JFAST_WEBSOCKET_", env_file=".env", extra="ignore"
    )

    # One channel for the whole service. Every worker subscribes to it and
    # filters locally; see the module docstring for what that costs.
    channel: str = "websocket.fanout"
    # Namespaces the Redis channel. Two environments sharing a Redis without
    # this is two environments delivering each other's messages.
    prefix: str = ""

    # Frames a connection may fall behind by. 32 is roughly a second of a
    # chatty feed; raising it raises the memory one stalled client can pin.
    send_buffer: int = 32
    # close      : disconnect the client and make it reconnect and resync.
    # drop_oldest: keep the newest frames and lose the older ones, for a feed
    #              where only the latest value matters.
    overflow: str = "close"

    # Below the idle timeout of the proxies in front of this (nginx and ALB
    # both default to 60s), so an idle connection is not cut by the path.
    heartbeat_seconds: float = 20.0
    # Two missed pongs. 0 disables it and leaves liveness to the WebSocket
    # protocol pings uvicorn already sends.
    idle_timeout_seconds: float = 60.0
    close_on_token_expiry: bool = True

    # Accepts the socket and waits for {"type": "auth", "token": "..."}. Off by
    # default: it means a connection exists before it is authorised.
    allow_first_message_auth: bool = False
    auth_deadline_seconds: float = 5.0


class WebSocketPlugin(Plugin):
    meta = PluginMeta(
        name="websocket",
        version="0.1.0",
        description="Authenticated WebSockets with a Redis backplane and a connection registry.",
        # Both are hard. `auth` because the handshake verifies a token and
        # there is no anonymous mode; `cache` because the Redis backplane is
        # what makes a message reach a socket on another worker, and without it
        # this is a single-process toy that looks like it scales.
        requires=("auth", "cache"),
        after=("observability", "channels"),
        provides=("websockets",),
        default_enabled=False,
        extra="jfastframework[server]",
        # Losing the backplane costs cross-worker delivery, not the service.
        health_critical=False,
    )
    Settings = WebSocketSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._hub: WebSocketHub | None = None
        self._backend: RedisBackend | None = None
        self._mounted: dict[str, SocketRoute] = {}

    def register(self, ctx: AppContext) -> None:
        settings: WebSocketSettings = self.settings
        auth = ctx.optional("auth")
        if auth is None:
            raise PluginError(
                "The websocket plugin needs the `auth` plugin: the handshake verifies a "
                'token and there is no anonymous mode. Add "auth" to [plugins].enabled.'
            )
        client = ctx.optional("cache.client")
        if client is None:
            raise PluginError(
                "The websocket plugin needs the `cache` plugin: Redis is the backplane "
                "that carries a message to the worker holding the recipient's socket. "
                'Add "cache" to [plugins].enabled.'
            )

        channel = Channel(
            settings.channel,
            description="WebSocket fan-out: one addressed message per worker.",
            backend="redis",
            required=("kind", "target", "data"),
        )
        self._backend = RedisBackend(client, prefix=settings.prefix)
        channel.bind(self._backend)

        registry = ctx.optional("channels")
        if registry is not None:
            # So `jfast describe` lists this channel with the declared ones
            # rather than leaving it invisible.
            registry.register(channel)

        self._hub = WebSocketHub(
            channel,
            verify=auth.verify_token,
            send_buffer=settings.send_buffer,
            overflow=settings.overflow,
            heartbeat_seconds=settings.heartbeat_seconds,
            idle_timeout_seconds=settings.idle_timeout_seconds,
            close_on_token_expiry=settings.close_on_token_expiry,
            allow_first_message_auth=settings.allow_first_message_auth,
            auth_deadline_seconds=settings.auth_deadline_seconds,
        )
        ctx.provide("websockets", self._hub)
        self._mount_pending(ctx)

    def _mount_pending(self, ctx: AppContext) -> None:
        """Mount endpoints declared with the module-level ``socket``.

        Called from both hooks because a module can be imported either side of
        ``register``: the app's own modules before it, a lazily imported router
        after it. Mounting is idempotent by path.
        """
        assert self._hub is not None
        hub = self._hub
        for route in pending_sockets():
            if route.path in self._mounted:
                continue
            self._mounted[route.path] = route
            ctx.app.add_api_websocket_route(route.path, _endpoint_for(hub, route), name=route.name)

    async def startup(self, ctx: AppContext) -> None:
        assert self._hub is not None
        # Subscribing here rather than in register(): register() must not do
        # I/O, and a worker that has not subscribed yet holds sockets it cannot
        # deliver to.
        await self._hub.start()
        self._mount_pending(ctx)

    async def shutdown(self, ctx: AppContext) -> None:
        if self._hub is not None:
            await self._hub.close_all()
        if self._backend is not None:
            await self._backend.close()

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._hub is None:
            return HealthReport.fail("websocket hub not built")
        return HealthReport.ok(
            f"{self._hub.registry.total} connection(s)",
            channel=self.settings.channel,
            **self._hub.registry.describe(),
        )

    def describe(self) -> dict[str, Any]:
        described = super().describe()
        # What is mounted, not what is pending: an agent reading this needs the
        # endpoints that exist, and the pending list is drained and reusable.
        described["sockets"] = [
            {"path": route.path, "scopes": list(route.scopes)}
            for route in sorted(self._mounted.values(), key=lambda route: route.path)
        ]
        return described


def _endpoint_for(hub: WebSocketHub, route: SocketRoute) -> Callable[[WebSocket], Awaitable[None]]:
    async def endpoint(websocket: WebSocket) -> None:
        await hub.serve(websocket, route.handler, scopes=route.scopes)

    return endpoint


def _token_from(socket: WebSocket) -> tuple[str | None, str | None]:
    """The bearer token and the subprotocol to echo, or (None, None).

    The query string is deliberately not one of the places looked at.
    """
    header = socket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None, None

    offered = [part.strip() for part in socket.headers.get("sec-websocket-protocol", "").split(",")]
    if len(offered) >= 2 and offered[0] == AUTH_SUBPROTOCOL and offered[1]:
        return offered[1], AUTH_SUBPROTOCOL
    return None, None


__all__ = [
    "AUTH_SUBPROTOCOL",
    "Connection",
    "ConnectionRegistry",
    "Envelope",
    "SocketRoute",
    "WebSocketHub",
    "WebSocketPlugin",
    "WebSocketSettings",
    "clear_pending",
    "pending_sockets",
    "socket",
]
