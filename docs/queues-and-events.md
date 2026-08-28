# Queues and events

Two different things, deliberately two different plugins.

| | `queue` | `events` |
| --- | --- | --- |
| A message means | "do this" | "this happened" |
| Consumers | exactly one wins | every group gets a copy |
| After consuming | gone | still there, replayable |
| Failure handling | retry, then dead-letter | offsets, replay |
| Backends | PostgreSQL, Redis, RabbitMQ | Kafka |
| Use for | send the email, resize the image | tell other services an order was paid |

Using a queue for events means adding another queue every time a new service
starts caring. Using a stream for jobs means reimplementing retries and
dead-lettering on top of offsets. Pick by which row you are in.

---

## Background jobs

```toml
[plugins]
enabled = ["observability", "database", "queue"]

[plugin.queue]
backend = "postgres"     # or "redis", "rabbitmq"
max_attempts = 3
```

Register a task, enqueue from a route:

```python
tasks = request.app.state.jfast.require("tasks")

@tasks.task("send_invoice_email")
async def send_invoice_email(payload: dict) -> None:
    ...

queue = request.app.state.jfast.require("queue")
await queue.enqueue(Job(task="send_invoice_email", payload={"invoice_id": 7}))
```

Run a worker:

```python
from jfastframework.queues import Worker

worker = Worker(queue, tasks, concurrency=4)
await worker.run()
```

`GET /queue/stats` reports depths and registered task names.

### Delivery is at-least-once. Handlers must be idempotent.

A worker can do the work and die before acknowledging. Then the job is
redelivered and the work happens twice. No backend here promises
exactly-once, because none of them can.

Charging a card twice is a bug in the handler, not in the queue. Key the side
effect on something stable — the invoice id, an idempotency key — and check
before acting.

### Choosing a backend

| | PostgreSQL (default) | Redis | RabbitMQ |
| --- | --- | --- | --- |
| Extra service | none | Redis | RabbitMQ |
| Enqueue in your transaction | **yes** | no | no |
| Latency | poll interval | microseconds | microseconds |
| Throughput ceiling | hundreds/sec | tens of thousands | very high |
| Routing, priorities, UI | no | no | yes |

**Start with PostgreSQL.** The transactional property is worth more than the
latency for most work: `INSERT` the order and enqueue "charge the card" in one
transaction, and a rollback takes the job with it. With Redis you can commit
the row, crash before the `LPUSH`, and the job simply never exists.

Move to Redis when the poll latency actually matters, and to RabbitMQ when you
need routing, priorities or an operator UI. Be able to name the number you hit.

### How each backend stays honest

**PostgreSQL** claims with `SELECT … FOR UPDATE SKIP LOCKED`, so concurrent
workers take different rows instead of blocking. A partial index covers exactly
the claim predicate, so accumulating dead jobs do not slow the queue down.

**Redis** uses `BLMOVE` into a per-worker processing list. A worker that dies
leaves its job visible for recovery; a naive `BRPOP` queue drops it. On startup
and shutdown the worker returns anything left in its own processing list.

**RabbitMQ** retries through a dead-letter exchange with a TTL: a rejected job
goes to a delay queue whose messages expire back onto the main queue. Sleeping
in the worker instead would hold a connection and lose the delay on restart.

### Retries

Exponential backoff, capped at five minutes, bounded by `max_attempts`. A job
that exhausts them goes to the dead-letter queue.

Both bounds matter. Uncapped backoff schedules the last retry days out, which
looks like the job vanished. Unbounded retries let one poison message occupy a
worker forever.

An **unknown task** is dead-lettered immediately, without retrying: no future
deploy makes it deliverable, and retrying hides the real problem behind a
growing queue.

### Task names are a wire contract

Jobs queued by yesterday's deploy are still in the queue when today's rolls
out. Rename a task and those jobs become undeliverable. Add the new name,
keep the old one until the queue has drained, then remove it.

---

## Events

```toml
[plugins]
enabled = ["observability", "events"]

[plugin.events]
bootstrap_servers = "localhost:9092"
consumer_group = "billing"
```

```python
events = request.app.state.jfast.require("events")

await events.publish("orders", Event(
    type="order.paid",
    data={"order_id": 7, "amount": "42.00"},
    key="order-7",          # partition key: order events stay ordered
))

@events.on("orders")
async def on_order(event: Event) -> None:
    if event.type == "order.paid":
        ...
```

### Partition keys are not optional

Kafka orders messages **within a partition**, not within a topic. Publish
events about one order without a key and two consumers can process
`order.paid` before `order.created`. Use the aggregate id as the key.

### Offsets are committed after handling

`enable_auto_commit` is off. The consumer commits after the handler returns, so
a crash mid-handler redelivers rather than skips — at-least-once again. A
handler that raises does not commit, so a poison message blocks its partition.
That is visible and fixable; silently skipping it is neither.

### Events are past tense and immutable

`order.paid`, not `pay_order`. An event says something happened; a command asks
for something to happen, and a command belongs in a queue. Once published, an
event is history: correct it with a new event, never by rewriting the old one.

---

## Infrastructure

Enabled plugins contribute their containers to the generated compose file:

```bash
jfast deploy compose --stdout        # one service
jfast workspace compose              # the whole workspace
```

RabbitMQ lands at offset +6, Kafka at +2 (KRaft mode — no ZooKeeper, one
container instead of two). The `postgres` and `redis` queue backends add no
container: they reuse the one their own plugin already declares.

---

## What is verified, and what is not

**Tested in CI:** the `Job` model, backoff and its cap, exhaustion,
dead-lettering, unknown-task handling, job timeouts, worker draining on
shutdown, and that an idle worker yields instead of busy-waiting — against an
in-memory backend implementing the same protocol.

**Not tested:** the PostgreSQL, Redis, RabbitMQ and Kafka backends against real
servers. The SQL and the client calls are written against documented behaviour
but have not been round-tripped in CI. An integration suite with real
containers is PLAN.md phase 6; until then, treat your first deployment of a
non-default backend as the test.
