# shared/, enums, and channels

Two features, one idea: **coupling that is easy to add, invisible once added,
and expensive to remove after a second person has copied the pattern.** Both
are therefore checked rather than agreed.

---

## Where an enum goes

```bash
jfast new enum InvoiceStatus --module invoice
jfast new enum Currency --shared
```

Run without a flag it asks, because the placement *is* the decision:

```
  Will more than one module use it?  yes puts it in shared/, no puts it in one module
```

You do not have to answer it correctly on day one. Start an enum in the module
that needs it, and the check tells you the day a second one wants it.

### The rule

**Modules do not import each other.** Two modules that reach into each other
are one module with a folder between them — neither can be extracted into a
service later, and a change to one breaks the other in a way no test covers.

```
modules/payment/service.py:41: cross-module: module 'payment' imports module 'invoice'
  (two modules that need the same thing should share it: move it to shared/enums.py)
```

Moving it to `shared/` clears the finding. The message names the file, so the
fix does not need a design discussion.

**The direction is one-way**, and that is checked too:

```
shared/enums.py:43: shared-direction: shared/ imports modules.invoice
  (the direction is one-way: modules use shared/, never the reverse,
   or the graph becomes a circle)
```

Without that second rule `shared/` becomes the place everything ends up, which
is the failure mode of every `utils` package ever written.

### What belongs in shared/

| Belongs | Does not |
| --- | --- |
| Enums two modules both speak | Anything only one module uses |
| Value objects and shared types | Anything that touches the database |
| Pure functions | Anything that makes an HTTP call |

The database line is the one that matters, and the generated `contracts.toml`
enforces it by forbidding `sqlalchemy` in `shared/`. **Two modules sharing a
repository is two modules sharing a table**, and that is how a set of services
becomes a distributed monolith with extra latency.

### Why `str, Enum`

Both templates use it, and the reason is not style. A plain `Enum` serialises
as `Status.DRAFT` down some paths and `"DRAFT"` down others; inheriting from
`str` makes a member a string everywhere — in JSON, in SQL, and in a log line.

And **the value is the wire format**. It is stored in a column, serialised into
an API response and read by a frontend. Renaming a member is free; changing its
value is a data migration.

Turn the whole thing off if you disagree:

```toml
[rules.placement]
enabled = false
```

---

## Channels

The pattern this replaces is a file of string constants imported wherever
somebody publishes:

```python
VINCULACION_COMPLETADA = "eventos:vinculacion_completada"
```

That works, and fails in three specific ways: nothing checks the payload, the
transport is welded to the call site, and nobody can list the channels a system
uses.

```python
# channels.py
from jfastframework.channels import Channel

VINCULACION_COMPLETADA = Channel(
    "eventos:vinculacion_completada",
    description="A linkage finished; downstream balances may be stale.",
    required=("vinculacion_id", "rfc"),
)

LARAVEL_CHEQUES = Channel("LARAVEL_CHEQUES_EVENTS", backend="redis")
```

```python
await VINCULACION_COMPLETADA.publish({"vinculacion_id": 7, "rfc": "AAA010101AAA"})

@VINCULACION_COMPLETADA.on
async def recalculate(payload):
    ...
```

### The payload is validated where it is built

```
ChannelError: Payload for 'eventos:vinculacion_completada' is missing rfc.
```

That failure belongs in the service that built the message, not in a worker
three services away that read a key which is no longer there.

`required` is a set of key names rather than a model on purpose. These payloads
cross language boundaries — Laravel publishes to some of them — so the contract
that can be enforced on both sides is *these keys are present*, not *this is a
pydantic model*.

### Backends are per channel

Mixing is the normal case, not an edge case:

| Backend | Use it when | Understand that |
| --- | --- | --- |
| `memory` | Default. Inside one process. | Delivery is to this process only — correct for a modular monolith, wrong the moment there are two replicas. |
| `redis` | Something else, in another language, publishes or subscribes. | Nothing is retained. A subscriber that is not connected never sees the message. |
| `kafka` | A consumer that was down has to catch up. | Retained and replayable, and needs the `events` plugin. |

The default needs no infrastructure at all, so publishing an event is not a
decision you have to make on day one. Moving a channel to Redis later is one
keyword on the declaration.

`redis` is for *this changed, refresh*. For *do this work*, use the queue — it
retries, backs off and dead-letters, and pub/sub does none of those.

### Listing them

```bash
jfast describe --json | jq '.plugins[] | select(.name=="channels") | .channels'
```

Which is the third failure fixed: the channels a system speaks are in one file
and one command, rather than spread across whichever modules happen to publish.
