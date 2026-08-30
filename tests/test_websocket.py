"""WebSockets: the handshake, the registry, and the message that must arrive once.

Most of these are about the failures that do not look like failures. A socket
that stays open after its token expired still works. A registry that keeps an
entry for a dead socket still answers. A message delivered twice still arrives.

The test that justifies the plugin is at the bottom: two workers, one real
Redis, and a message published on one reaching a socket on the other exactly
once. It is skipped when no Redis is reachable, and the skip is the honest
answer -- see STATUS.md.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import APIRouter, Request, WebSocketDisconnect
from starlette.datastructures import Headers

from jfastframework.auth import Principal, TokenError, issue
from jfastframework.channels import Channel, MemoryBackend
from jfastframework.plugins.builtin.websocket import (
    AUTH_SUBPROTOCOL,
    Connection,
    WebSocketHub,
    WebSocketPlugin,
    clear_pending,
    pending_sockets,
    socket,
)
from jfastframework.testing import build_test_app

# Same variable `test_ratelimit.py` uses, so one Redis serves both. The default
# is the standard port, which is what a CI `services: redis` publishes.
REDIS_URL = os.environ.get("JFAST_TEST_REDIS_URL", "redis://localhost:6379/0")
SECRET = "a-test-secret-long-enough-for-sha256-at-least-32-bytes"
ISSUER = "https://id.example.com/"
AUDIENCE = "realtime"


# -- doubles ------------------------------------------------------------


class FakeSocket:
    """Stands in for a Starlette ``WebSocket``.

    ``stalled`` is the client that stopped reading: every send blocks until
    the test releases it, which is what fills a bounded send buffer.
    """

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
        stalled: bool = False,
    ) -> None:
        self.headers = Headers(headers or {})
        self.query_params = query_params or {}
        self.sent: list[dict[str, Any]] = []
        self.accepted = False
        self.subprotocol: str | None = None
        self.closed: tuple[int, str] | None = None
        self.inbound: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self._drain = asyncio.Event()
        if not stalled:
            self._drain.set()

    def release(self) -> None:
        self._drain.set()

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = True
        self.subprotocol = subprotocol

    async def send_text(self, data: str) -> None:
        await self._drain.wait()
        if self.closed is not None:
            raise RuntimeError("socket is closed")
        self.sent.append(json.loads(data))

    async def receive_text(self) -> str:
        item = await self.inbound.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [frame["data"] for frame in self.sent if frame.get("type") == "message"]


TOKENS: dict[str, Principal] = {}


def token_for(
    subject: str,
    *,
    tenant_id: str | None = None,
    scopes: tuple[str, ...] = (),
    expires_in: int = 300,
    claims: dict[str, Any] | None = None,
) -> str:
    token = f"token-{subject}-{len(TOKENS)}"
    TOKENS[token] = Principal(
        subject=subject,
        tenant_id=tenant_id,
        scopes=frozenset(scopes),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        claims=claims or {},
    )
    return token


async def verify(token: str) -> Principal:
    try:
        return TOKENS[token]
    except KeyError:
        raise TokenError("unknown token") from None


def subprotocol_headers(token: str) -> dict[str, str]:
    return {"sec-websocket-protocol": f"{AUTH_SUBPROTOCOL}, {token}"}


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    clear_pending()
    TOKENS.clear()
    yield
    clear_pending()


async def _hub(**kwargs: Any) -> WebSocketHub:
    channel = Channel("ws.fanout")
    channel.bind(MemoryBackend())
    hub = WebSocketHub(channel, verify=verify, **kwargs)
    await hub.start()
    return hub


async def _serve(hub: WebSocketHub, sock: FakeSocket, **kwargs: Any) -> asyncio.Task[None]:
    async def run() -> None:
        await hub.serve(sock, listen, **kwargs)  # type: ignore[arg-type]

    return asyncio.create_task(run())


async def listen(conn: Connection) -> None:
    await conn.listen()


async def _connected(hub: WebSocketHub, subject: str, tenant: str | None = None) -> Connection:
    for _ in range(200):
        found = hub.registry.for_subject(subject, tenant_id=tenant)
        if found:
            return found[0]
        await asyncio.sleep(0)
    raise AssertionError(f"no connection for {subject!r} appeared")


# -- the handshake ------------------------------------------------------


async def test_a_token_in_the_query_string_is_never_read() -> None:
    """The route this plugin refuses: a query parameter lands in every log."""
    hub = await _hub()
    sock = FakeSocket(query_params={"token": token_for("alice")})

    await hub.serve(sock, listen)  # type: ignore[arg-type]

    assert sock.accepted is False
    assert sock.closed is not None
    assert sock.closed[0] == 1008


async def test_the_handshake_reads_the_token_from_the_websocket_subprotocol() -> None:
    hub = await _hub()
    token = token_for("alice")
    sock = FakeSocket(headers=subprotocol_headers(token))

    task = await _serve(hub, sock)
    conn = await _connected(hub, "alice")

    assert sock.accepted is True
    # A browser aborts the connection unless the server echoes one it offered.
    assert sock.subprotocol == AUTH_SUBPROTOCOL
    assert conn.principal.subject == "alice"

    sock.inbound.put_nowait(WebSocketDisconnect(1000))
    await asyncio.wait_for(task, 2)


async def test_the_handshake_also_reads_an_authorization_header() -> None:
    """Browsers cannot set it; every other client can, and prefers to."""
    hub = await _hub()
    sock = FakeSocket(headers={"authorization": f"Bearer {token_for('alice')}"})

    task = await _serve(hub, sock)
    await _connected(hub, "alice")
    assert sock.subprotocol is None

    sock.inbound.put_nowait(WebSocketDisconnect(1000))
    await asyncio.wait_for(task, 2)


async def test_a_refresh_token_cannot_open_a_socket() -> None:
    hub = await _hub()
    token = token_for("alice", claims={"typ": "refresh"})

    await hub.serve(FakeSocket(headers=subprotocol_headers(token)), listen)  # type: ignore[arg-type]

    assert hub.registry.total == 0


async def test_a_socket_without_the_required_scope_is_refused() -> None:
    hub = await _hub()
    sock = FakeSocket(headers=subprotocol_headers(token_for("alice", scopes=("chat:read",))))

    await hub.serve(sock, listen, scopes=("chat:write",))  # type: ignore[arg-type]

    assert sock.accepted is False
    assert sock.closed == (1008, "missing scope")


# -- the registry -------------------------------------------------------


async def test_the_registry_forgets_a_socket_that_died_abruptly() -> None:
    """1006, no close frame. The entry has to go anyway or it leaks per socket."""
    hub = await _hub()
    sock = FakeSocket(headers=subprotocol_headers(token_for("alice")))

    task = await _serve(hub, sock)
    await _connected(hub, "alice")

    sock.inbound.put_nowait(WebSocketDisconnect(1006))
    await asyncio.wait_for(task, 2)

    assert hub.registry.for_subject("alice") == ()
    assert hub.registry.total == 0


async def test_a_socket_that_fails_on_send_is_forgotten_too() -> None:
    hub = await _hub()
    sock = FakeSocket(headers=subprotocol_headers(token_for("alice")))

    task = await _serve(hub, sock)
    await _connected(hub, "alice")

    await sock.close(1006, "gone")
    await hub.send("alice", {"n": 1})
    await asyncio.wait_for(task, 2)

    assert hub.registry.total == 0


async def test_leaving_a_room_does_not_leave_the_room_behind() -> None:
    hub = await _hub()
    sock = FakeSocket(headers=subprotocol_headers(token_for("alice")))

    async def handler(conn: Connection) -> None:
        await conn.join("orders")
        await conn.listen()

    task = asyncio.create_task(hub.serve(sock, handler))  # type: ignore[arg-type]
    await _connected(hub, "alice")

    sock.inbound.put_nowait(WebSocketDisconnect(1006))
    await asyncio.wait_for(task, 2)

    assert hub.registry.rooms == ()


# -- delivery -----------------------------------------------------------


async def test_every_socket_of_a_subject_gets_exactly_one_copy() -> None:
    """Two tabs, one message each -- and the publishing worker must not
    deliver locally *and* over the backplane."""
    hub = await _hub()
    first = FakeSocket(headers=subprotocol_headers(token_for("alice")))
    second = FakeSocket(headers=subprotocol_headers(token_for("alice")))

    tasks = [await _serve(hub, first), await _serve(hub, second)]
    for _ in range(200):
        if len(hub.registry.for_subject("alice")) == 2:
            break
        await asyncio.sleep(0)

    await hub.send("alice", {"n": 1})
    await hub.send("alice", {"n": 2})
    await asyncio.sleep(0.05)

    assert first.messages == [{"n": 1}, {"n": 2}]
    assert second.messages == [{"n": 1}, {"n": 2}]

    for sock, task in zip([first, second], tasks, strict=True):
        sock.inbound.put_nowait(WebSocketDisconnect(1000))
        await asyncio.wait_for(task, 2)


async def test_a_message_never_crosses_a_tenant() -> None:
    hub = await _hub()
    acme = FakeSocket(headers=subprotocol_headers(token_for("alice", tenant_id="acme")))
    other = FakeSocket(headers=subprotocol_headers(token_for("alice", tenant_id="globex")))

    tasks = [await _serve(hub, acme), await _serve(hub, other)]
    await _connected(hub, "alice", "acme")
    await _connected(hub, "alice", "globex")

    await hub.send("alice", {"secret": True}, tenant_id="acme")
    await asyncio.sleep(0.05)

    assert acme.messages == [{"secret": True}]
    assert other.messages == []

    for sock, task in zip([acme, other], tasks, strict=True):
        sock.inbound.put_nowait(WebSocketDisconnect(1000))
        await asyncio.wait_for(task, 2)


async def test_a_room_is_scoped_to_the_tenant_that_joined_it() -> None:
    """Same room name, two tenants. A shared name is a cross-tenant leak."""
    hub = await _hub()
    acme = FakeSocket(headers=subprotocol_headers(token_for("a", tenant_id="acme")))
    globex = FakeSocket(headers=subprotocol_headers(token_for("b", tenant_id="globex")))

    async def handler(conn: Connection) -> None:
        await conn.join("orders")
        await conn.listen()

    tasks = [
        asyncio.create_task(hub.serve(acme, handler)),  # type: ignore[arg-type]
        asyncio.create_task(hub.serve(globex, handler)),  # type: ignore[arg-type]
    ]
    for _ in range(200):
        if hub.registry.total == 2:
            break
        await asyncio.sleep(0)

    await hub.send_to_room("orders", {"order": 1}, tenant_id="acme")
    await asyncio.sleep(0.05)

    assert acme.messages == [{"order": 1}]
    assert globex.messages == []

    for sock, task in zip([acme, globex], tasks, strict=True):
        sock.inbound.put_nowait(WebSocketDisconnect(1000))
        await asyncio.wait_for(task, 2)


# -- backpressure -------------------------------------------------------


async def test_a_client_that_stops_reading_is_disconnected_not_buffered() -> None:
    """The default. An unbounded buffer is how one slow client takes a worker
    down; dropping silently is how a client's state diverges with no signal."""
    hub = await _hub(send_buffer=2)
    sock = FakeSocket(headers=subprotocol_headers(token_for("alice")), stalled=True)

    task = await _serve(hub, sock)
    await _connected(hub, "alice")

    for n in range(20):
        await hub.send("alice", {"n": n})

    sock.release()
    await asyncio.wait_for(task, 2)

    assert sock.closed is not None
    assert sock.closed[0] == 1013
    assert hub.registry.total == 0


async def test_a_telemetry_stream_can_choose_to_drop_instead() -> None:
    """For a stream where only the newest value matters, dropping beats closing."""
    hub = await _hub(send_buffer=2, overflow="drop_oldest")
    sock = FakeSocket(headers=subprotocol_headers(token_for("alice")), stalled=True)

    task = await _serve(hub, sock)
    conn = await _connected(hub, "alice")

    for n in range(20):
        await hub.send("alice", {"n": n})

    assert conn.dropped > 0
    assert sock.closed is None

    sock.release()
    await asyncio.sleep(0.05)
    assert sock.messages[-1] == {"n": 19}

    sock.inbound.put_nowait(WebSocketDisconnect(1000))
    await asyncio.wait_for(task, 2)


# -- liveness and expiry ------------------------------------------------


async def test_a_silent_client_is_closed_when_the_idle_timeout_passes() -> None:
    """A dropped TCP connection is not observable without this."""
    hub = await _hub(heartbeat_seconds=0.01, idle_timeout_seconds=0.03)
    sock = FakeSocket(headers=subprotocol_headers(token_for("alice")))

    task = await _serve(hub, sock)
    await asyncio.wait_for(task, 2)

    assert sock.closed is not None
    assert sock.closed[0] == 1001
    assert any(frame.get("type") == "ping" for frame in sock.sent)


async def test_a_pong_keeps_the_connection_alive() -> None:
    hub = await _hub(heartbeat_seconds=0.01, idle_timeout_seconds=0.08)
    sock = FakeSocket(headers=subprotocol_headers(token_for("alice")))

    task = await _serve(hub, sock)
    conn = await _connected(hub, "alice")

    for _ in range(10):
        sock.inbound.put_nowait(json.dumps({"type": "pong"}))
        await asyncio.sleep(0.01)

    assert not task.done()
    # A pong is transport bookkeeping and must not reach the application.
    assert conn.received == 0

    sock.inbound.put_nowait(WebSocketDisconnect(1000))
    await asyncio.wait_for(task, 2)


async def test_a_socket_does_not_outlive_the_token_that_opened_it() -> None:
    hub = await _hub(heartbeat_seconds=0.01)
    token = token_for("alice", expires_in=-1)
    sock = FakeSocket(headers=subprotocol_headers(token))

    task = await _serve(hub, sock)
    await asyncio.wait_for(task, 2)

    assert sock.closed == (1008, "token expired")


# -- declaration --------------------------------------------------------


def test_the_decorator_defers_mounting_to_the_plugin() -> None:
    """Importing a module must not need an app, and a one-line-wide
    registration window is the bug this pattern exists to prevent."""

    @socket("/ws/notifications", scopes=("chat:read",))
    async def endpoint(conn: Connection) -> None: ...

    declared = pending_sockets()
    assert [route.path for route in declared] == ["/ws/notifications"]
    assert declared[0].scopes == ("chat:read",)


def test_the_plugin_is_discoverable_as_an_entry_point() -> None:
    from jfastframework.plugins import registry

    assert registry.discover().get("websocket") is WebSocketPlugin


def test_the_plugin_declares_what_it_cannot_work_without() -> None:
    """Redis is not a suggestion: without it there is no cross-worker delivery."""
    assert WebSocketPlugin.meta.default_enabled is False
    assert set(WebSocketPlugin.meta.requires) == {"auth", "cache"}


# -- two workers, one Redis ---------------------------------------------


def _redis_reachable() -> bool:
    try:
        import redis
    except ImportError:  # pragma: no cover - the dev extra installs it
        return False
    try:
        client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1)
        client.ping()
        client.close()
    except Exception:  # noqa: BLE001
        return False
    return True


needs_redis = pytest.mark.skipif(
    not _redis_reachable(), reason=f"no Redis at {REDIS_URL}; cross-worker delivery is unproven"
)


def _mint(subject: str) -> str:
    token, _, _ = issue(
        subject,
        key=SECRET,
        algorithm="HS256",
        lifetime=timedelta(minutes=5),
        audience=AUDIENCE,
        issuer=ISSUER,
    )
    return token


def _worker(router: APIRouter) -> Any:
    return build_test_app(
        plugins=["observability", "cache", "auth", "websocket"],
        extra_plugins=[WebSocketPlugin],
        routers=[router],
        raw={
            "plugin": {
                "cache": {"url": REDIS_URL},
                "auth": {
                    "mode": "secret",
                    "secret": SECRET,
                    "algorithms": ["HS256"],
                    "issuer": ISSUER,
                    "audience": AUDIENCE,
                    "mount_router": False,
                    "check_revocation": False,
                },
                "websocket": {"channel": "test.ws.fanout", "heartbeat_seconds": 60},
            }
        },
    )


def _next_message(session: Any) -> dict[str, Any]:
    while True:
        frame = session.receive_json()
        if frame.get("type") == "message":
            return dict(frame["data"])


@needs_redis
def test_a_message_published_on_one_worker_reaches_a_socket_on_the_other() -> None:
    """The test that justifies the plugin existing.

    Two apps in one process, each with its own Redis subscription, each holding
    one socket. Delivery is asserted exactly once by sequence number: a
    duplicate would show up as the next frame repeating the previous one.
    """
    from starlette.testclient import TestClient

    clear_pending()

    @socket("/ws")
    async def endpoint(conn: Connection) -> None:
        await conn.listen()

    api = APIRouter()

    @api.post("/say/{subject}/{n}")
    async def say(subject: str, n: int, request: Request) -> dict[str, int]:
        hub: WebSocketHub = request.app.state.jfast.require("websockets")
        await hub.send(subject, {"n": n})
        return {"n": n}

    worker_a = _worker(api)
    worker_b = _worker(api)

    with TestClient(worker_a) as client_a, TestClient(worker_b) as client_b:
        alice = client_b.websocket_connect("/ws", subprotocols=[AUTH_SUBPROTOCOL, _mint("alice")])
        bob = client_a.websocket_connect("/ws", subprotocols=[AUTH_SUBPROTOCOL, _mint("bob")])
        with alice as ws_alice, bob as ws_bob:
            # Published on A, the socket is on B: it crosses Redis or it does
            # not arrive at all.
            client_a.post("/say/alice/1")
            client_a.post("/say/alice/2")
            assert _next_message(ws_alice) == {"n": 1}
            assert _next_message(ws_alice) == {"n": 2}

            # Published on the worker that holds the socket: still exactly one
            # copy. Delivering locally *and* over the backplane is the bug, and
            # the next frame being 4 rather than a second 3 is what rules it
            # out -- one assertion on 3 alone would pass with a duplicate
            # sitting behind it.
            client_b.post("/say/alice/3")
            client_b.post("/say/alice/4")
            assert _next_message(ws_alice) == {"n": 3}
            assert _next_message(ws_alice) == {"n": 4}

            # Bob's first frame is bob's: he received none of alice's.
            client_b.post("/say/bob/9")
            assert _next_message(ws_bob) == {"n": 9}


@needs_redis
def test_a_worker_refuses_a_socket_whose_token_it_cannot_verify() -> None:
    from starlette.testclient import TestClient

    clear_pending()

    @socket("/ws")
    async def endpoint(conn: Connection) -> None:
        await conn.listen()

    worker = _worker(APIRouter())

    with (
        TestClient(worker) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws", subprotocols=[AUTH_SUBPROTOCOL, "not-a-token"]),
    ):
        pass
