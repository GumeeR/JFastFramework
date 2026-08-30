# WebSockets

A message is addressed to a **person**, not to a socket. Which worker happens to
hold that person's connection is not something your code should know.

```toml
[plugins]
enabled = ["observability", "cache", "auth", "websocket"]

[plugin.websocket]
channel = "websocket.fanout"
send_buffer = 32
heartbeat_seconds = 20
idle_timeout_seconds = 60
```

Declare an endpoint:

```python
from jfastframework.plugins.builtin.websocket import Connection, socket

@socket("/ws/notifications", scopes=("notifications:read",))
async def notifications(conn: Connection) -> None:
    await conn.join("orders")
    await conn.listen()
```

Send to a person, from any route, on any worker:

```python
hub = ctx.require("websockets")
await hub.send("user-42", {"kind": "invoice.paid", "id": 7})
await hub.send_to_room("orders", {"kind": "order.moved"}, tenant_id="acme")
```

Off by default. `requires = ("auth", "cache")` is enforced by the kernel, not
suggested here: the handshake verifies a token, and Redis is the backplane that
carries a message to the worker holding the recipient's socket. Without it this
is a single-process toy that looks like it scales.

## How a message crosses workers

Every worker subscribes to **one** channel and delivers only to the sockets it
holds. A publish never delivers locally; local delivery happens only through the
subscription.

```
POST /invoices        worker A ──publish──▶ Redis ──▶ worker A  (holds nobody)
   (on worker A)                              │
                                              └──▶ worker B  (holds user-42) ──▶ socket
```

That single rule is what makes it **exactly once per socket**. Delivering
locally *and* over the backplane would give every socket on the publishing
worker two copies of the same message — which is the bug this design exists to
prevent, and the one the test suite asserts against.

Because no worker is special, **the load balancer needs no sticky sessions**.
Any worker can accept any connection.

The cost of one channel is that every worker sees every message and discards
most of them. Per-subject Redis channels would cut that traffic, at the price of
a `SUBSCRIBE` and `UNSUBSCRIBE` per connection. That trade is worth revisiting
above a few thousand messages a second, and not before.

One subject with two sockets — two browser tabs — gets one copy **per socket**.
Exactly-once is per connection, not per person.

## The handshake

A browser cannot set an `Authorization` header on a WebSocket. The two usual
answers are both worse than they look:

| Route | What it costs |
| --- | --- |
| Token in the query string | Written into every access log, proxy trace and browser history entry it passes through, and it cannot be rotated out of any of them. |
| Token in the first message | The socket exists, and holds a file descriptor, before anybody has proved who they are. |

**This plugin reads the token from `Sec-WebSocket-Protocol`, and never from the
query string.** A browser can offer subprotocols, so the token travels in a
header:

```javascript
const ws = new WebSocket("wss://api.example.com/ws/notifications", [
  "jfast.auth.bearer",
  accessToken,
]);
```

The server verifies the second entry and echoes back `jfast.auth.bearer` — a
browser aborts the connection if the server selects a subprotocol it did not
offer.

Non-browser clients — a worker, a mobile app, a Go service — can set a real
header, so `Authorization: Bearer …` is read first and preferred.

Verification goes through the `auth` plugin's `verify_token`, so JWKS rotation,
issuer, audience, algorithm pinning and revocation all apply exactly as they do
to an HTTP request. A refresh token is refused: it verifies like any other, and
without that check a 30-day token would open a socket wherever an access token
would.

**The case the first message covers** is a client that controls neither header.
It is available, and off by default:

```toml
[plugin.websocket]
allow_first_message_auth = true
auth_deadline_seconds = 5
```

With it on, the socket is accepted and the server waits for
`{"type": "auth", "token": "…"}`. Until that arrives the connection is not in
the registry, receives nothing, and is closed when the deadline passes. The
deadline is what bounds the unauthenticated window; that window is the reason
this is not the default.

Rejection happens **before** `accept()`, so the HTTP handshake is answered with
a refusal and no socket is ever upgraded.

## Backpressure: a slow client is disconnected, not buffered

Every connection has a bounded outbound queue drained by a writer task. Sends
never touch the socket directly — otherwise a publisher would await a TCP write
to the slowest client in the fan-out.

When that queue fills, there are three possible answers and only one default:

| Policy | Behaviour | When |
| --- | --- | --- |
| `close` *(default)* | Close with **1013 Try Again Later**. The client reconnects and re-reads its state. | Anything where a missing message changes what the user sees. |
| `drop_oldest` | Keep the newest frames, discard the oldest, count them on `conn.dropped`. | A telemetry feed where only the latest value matters. |
| unbounded | Not offered. | — |

An unbounded buffer is how one slow client takes a worker down: memory grows
until the process dies, and it dies serving everyone. Dropping silently is
worse in a different way — the client's state diverges from the server's with no
signal that it happened. Disconnecting is loud, and the client already has to
handle a reconnect.

`send_buffer = 32` is roughly a second of a chatty feed. Raising it raises the
memory a single stalled client can pin.

## Heartbeats

A dropped TCP connection is not observable from the ASGI layer: the socket looks
writable for as long as the kernel buffer accepts writes. **Only inbound traffic
proves the peer is alive**, which is why the timeout is on receiving and not on
sending.

- The server sends `{"type": "ping"}` every `heartbeat_seconds` (default 20).
- A connection with **no inbound frame at all** for `idle_timeout_seconds`
  (default 60) is closed with **1001**. That is two missed pongs before anything
  happens.
- Any inbound frame counts as liveness, not just a pong. A chatty client is
  never disconnected for failing to implement one.

Clients should answer:

```javascript
ws.onmessage = (event) => {
  const frame = JSON.parse(event.data);
  if (frame.type === "ping") return ws.send(JSON.stringify({ type: "pong" }));
  handle(frame.data);
};
```

A pong never reaches your handler — it is transport bookkeeping, and a handler
that has to filter it is a handler that will forget to.

20 seconds is deliberately under the idle timeout of the proxies that sit in
front of this: nginx `proxy_read_timeout` and an AWS ALB both default to 60.
Set `idle_timeout_seconds = 0` to disable the application-level timeout and rely
on the protocol-level pings uvicorn already sends (`--ws-ping-interval`), which
browsers answer without any application code.

## Auth expiry mid-connection

**A socket does not outlive the token that opened it.** When the access token's
`exp` passes, the connection is closed with **1008**, and the client refreshes
and reconnects.

The alternative — leaving it open — means a socket opened with a 15-minute token
is still streaming eight hours later under an authorization nothing ever
re-checked, and a logout or a revocation could never reach it. A reconnect costs
one round trip; the other option costs the meaning of the token lifetime.

```toml
[plugin.websocket]
close_on_token_expiry = false   # only if you know why
```

A token with no `exp` is never aged out, because there is nothing to age.

## Reconnection: messages sent while disconnected are lost

Stated plainly, because the transport does not support the alternative:

- Redis pub/sub retains nothing. A subscriber that is not connected when a
  message is published never sees it.
- Nothing here buffers per client.
- There is no replay, no acknowledgement, and **no ordering guarantee across a
  reconnect**.

So: a message published while a client was between sockets is **gone**. Design
for it — have the client re-read its state on connect rather than assume the
stream is a complete history:

```python
@socket("/ws/notifications")
async def notifications(conn: Connection) -> None:
    await conn.send({"kind": "resync", "unread": await unread_for(conn.subject)})
    await conn.listen()
```

Anything that must not be lost belongs in the queue plugin, which has retries
and a dead-letter, not here.

## Tenancy

The registry is keyed by `(tenant_id, subject)`, and the tenant comes from the
**verified token**, never from the client. Two consequences:

- A room name is scoped to the tenant that joined it. `orders` in `acme` and
  `orders` in `globex` are different rooms, so a client cannot subscribe across
  tenants by guessing a name.
- `hub.send("alice", …)` without a tenant reaches only connections whose token
  carried none. In a multi-tenant service the tenant is part of the address:

```python
await hub.send("alice", {"kind": "invoice.paid"}, tenant_id="acme")
```

## The connection registry

`conn.join(room)` / `conn.leave(room)` manage room membership; everything else
is automatic. A connection is removed from the subject index **and every room it
joined** in a `finally`, so an abrupt disconnect — 1006, no close frame — leaves
nothing behind. Removing it from only the subject index is the leak that shows
up in production as memory that grows with connection churn.

`GET /health` reports the live count:

```json
{ "detail": "3 connection(s)", "connections": 3, "subjects": 2, "rooms": 1 }
```

## What the client receives

Two frame shapes, both JSON objects:

```json
{ "type": "message", "room": null, "data": { "kind": "invoice.paid", "id": 7 } }
{ "type": "ping" }
```

`room` is the room name for a room message and `null` for one addressed to a
subject. Your payload is always under `data`, so a payload with its own `type`
key cannot collide with the transport's.

## Proxies

The upgrade has to survive the path. The generated Caddy config and the gateway
plugin pass it through; a hand-written nginx needs it spelled out:

```nginx
location /ws/ {
    proxy_pass http://app;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 300s;   # or the heartbeat has to be under 60
}
```

On Kubernetes, an Ingress that terminates idle connections at 60 seconds will
cut a WebSocket that is otherwise healthy. Either raise the timeout
(`nginx.ingress.kubernetes.io/proxy-read-timeout`) or leave `heartbeat_seconds`
under it — the default 20 already is.

## Maturity

`alpha`. See [STATUS.md](../STATUS.md) for what that means and what has not been
run.

The cross-worker test — two app instances, one real Redis, a socket on each,
exactly-once asserted by sequence number — exists in `tests/test_websocket.py`
and passes against `redis:7-alpine`. It **skips** unless a Redis is reachable,
and CI does not currently start one, so this has not been proven by a CI job.
That is the promotion gate, and it has not been met.
