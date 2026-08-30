# Workspaces and the API gateway

A single service needs no workspace. The moment there are two, three questions
appear that per-service config cannot answer:

* which ports are already taken;
* what the frontend should call;
* whether anything sits in front of them.

`jfast.workspace.toml` answers all three, and it is the only file that knows
the whole system.

---

## Resources: datastores with names

A workspace used to record datastores on a service by *type*:

```toml
datastores = ["database", "cache"]
```

Two things followed from that, and both were bugs wearing the costume of a
design. The generated compose file created a `billing-database` container and
**nothing wrote the DSN that pointed at it** -- the connection string stayed
hand-maintained in a `.env` beside a container that was generated, which is
exactly where drift lives. And a second PostgreSQL could not be expressed at
all, because there was no name to hang the second instance on.

A resource has a name. A service binds to it under a variable.

```toml
[[workspace.resources]]
name = "core-db"
type = "postgres"
port = 8900
database = "core"

[[workspace.resources]]
name = "shared-redis"
type = "redis"
port = 8901

[[workspace.services]]
name = "billing"
kind = "api"
port = 8010
path = "billing"
uses = [
  { resource = "core-db", as = "JFAST_DB_DSN" },
  { resource = "shared-redis", as = "JFAST_CACHE_URL" },
]
```

`as` defaults from the type -- a `postgres` resource lands in `JFAST_DB_DSN`,
which is what `DatabaseSettings` reads -- so the common case needs no
configuration to be correct.

### The commands

```bash
jfast workspace resource analytics-db --type postgres --database analytics
jfast link billing analytics-db --as JFAST_ANALYTICS_DSN
jfast link catalog shared-redis
jfast unlink catalog catalog-cache
jfast workspace resource catalog-cache --remove
jfast workspace validate
jfast workspace env          # writes every .env from the bindings
jfast workspace compose      # one container per resource
jfast workspace graph        # mermaid, edges labelled with the variable
```

Types are `postgres`, `redis`, `mongo` and `qdrant`. Resources take ports from
a band starting at `base_port + 900`, above the base port and below the first
service block, so a resource can never land inside a service's ten-port block.

### Containers a plugin owns

Those four types are the datastores the workspace names and shares. Everything
else a service needs comes from its plugins: `events` owns a Kafka broker,
`storage` can own MinIO, `queue` on the RabbitMQ backend owns a broker of its
own. None of them is a resource, and for a while `jfast workspace compose`
emitted the same file whether the plugin was enabled or not.

It now reads each service's `jfast.toml` -- the plugin list lives there, not in
the workspace file -- and emits what those plugins declare:

- containers from `infra()`, published inside that service's ten-port block,
  with `depends_on` wired to their healthcheck where they have one;
- a volume per local `storage` disk, mounted into the service's own container.
  Without it the uploads land in the container's filesystem and the next
  `docker build` throws them away, while the rows referencing them stay;
- **nothing** for `database`, `cache`, `mongo` and `qdrant`. Those four declare
  `infra()` as well, but the resource graph already owns their containers, and
  emitting both would stand a second, anonymous PostgreSQL beside the one every
  generated DSN points at.

Two services enabling `events` get **one** broker, not two. A broker advertises
its own container name as the address clients reconnect to, so a second copy
under a different name would advertise an address that does not reach it.

A plugin the generator cannot read -- an extra missing from *this* environment,
a settings block it rejects -- is a warning on stderr, not silence and not a
crash. Silence was the original defect: the container was simply not in the
file, and nothing said so.

A service with no `jfast.toml` (a Go service, a directory nothing has generated
yet) is skipped, and a workspace held only in memory has nothing to read.

### What is refused, and why

```
$ jfast link billing analytics-db
'billing' already binds 'core-db' to JFAST_DB_DSN. Two resources cannot share
one variable -- pass --as to choose another.
```

Two databases both defaulting to `JFAST_DB_DSN` is the ordinary way to get a
service quietly talking to the wrong one. `jfast workspace validate` catches
the rest: a port claimed twice, a binding to a resource that does not exist, a
resource nobody uses.

### Credentials

One password per resource, generated into the workspace `.env` by `jfast
workspace env` and never overwritten once set -- rotating a password is a
decision, and silently changing one locks a running container out of its own
volume. `jfast workspace init` adds `.env` to `.gitignore` at the same time, so
it cannot be committed by accident.

Each service's generated `.env` references the secret rather than repeating it:

```
JFAST_DB_DSN=postgresql+asyncpg://app:${CORE_DB_PASSWORD}@core-db:5432/core
```

### Coming from a 0.1 workspace

Nothing breaks. A service with the old `datastores` list still generates the
same containers on the same ports; the resources are simply implied rather than
named. When you want the explicit form:

```bash
jfast workspace migrate-resources
```

Ports are preserved, so the compose file it produces afterwards is the one it
produced before. Running it twice changes nothing.


## Start one

```bash
jfast workspace init cometax
```

From then on, every service created at or below that directory registers
itself, takes the next free port block, and appears in `jfast workspace list`.

```bash
jfast new service billing --with database,cache
jfast new service catalog --with qdrant,rag
jfast new service admin --kind spa --frontend vue
jfast workspace list
```

```
workspace : cometax
api url   : http://localhost:8030

billing          api      :8010    billing
catalog          api      :8020    catalog
gateway          gateway  :8030    gateway
admin            spa      :8040    admin  (vue)
```

## Port blocks, not ports

Each service owns ten consecutive ports. That is not tidiness: the plugins
*inside* a service claim offsets in that block — PostgreSQL at +1, Redis at +3,
Qdrant at +7 and +8. Handing the next service a consecutive port would make two
services fight over the same database container port.

`--port` overrides the allocation; taking one already in use is refused with
the next free block in the message.

---

## The gateway appears on its own

```bash
jfast new service catalog --with qdrant,rag
```

```
The workspace now has 2 backends and no gateway.
Generating one so clients need a single hostname:
  created  gateway/jfast.toml
  ...
Gateway on port 8030, routing 2 backend(s):
  /billing         -> http://billing:8010
  /catalog         -> http://catalog:8020
```

**One backend does not get a gateway.** It would add a network hop and an
outage surface for nothing. Two is where clients start having to know too many
hostnames, so that is the threshold.

Frontends do not count as backends: an API and a Vue app are still one thing to
call.

### Refresh it after adding a service

```bash
jfast workspace gateway --force
```

The routes in `gateway/jfast.toml` are derived from the workspace file. Do not
hand-edit them — regenerate, the same way you regenerate compose.

### What the gateway does per request

- Forwards method, query, headers and body to the matching upstream.
- Strips hop-by-hop headers (`connection`, `transfer-encoding`, `host`…) —
  they describe one connection and must not cross to the next.
- Propagates `X-Request-ID`, so one trace survives the hop and the upstream's
  logs correlate with the client's request. Sets `X-Forwarded-Host` / `-Proto`.
- Maps an unreachable upstream to `502` and a slow one to `504`, both as
  `application/problem+json`.

### What it deliberately does not do

**No catch-all route.** Only configured prefixes are proxied, so `/health`,
`/ready` and `/metrics` still belong to the gateway itself. A catch-all would
proxy the gateway's own liveness probe to whichever service sorted first.

**No upstream probing in `/ready`.** A gateway whose upstream is restarting is
still doing its job. Probing upstreams there turns one service's restart into
a whole-system readiness failure.

**No business logic.** Auth, rate limiting and caching are plugins you enable
on the gateway. Domain code is not — put it there and every team ships through
your gateway's deploy queue.

---

## What the frontend calls

```bash
jfast workspace env
```

Rewrites every frontend's `.env`:

```
VITE_API_URL=http://localhost:8030
```

The value is the gateway when there is one and the single backend when there is
not. That is exactly the string that goes stale by hand the day a gateway
appears, which is why it is generated.

---

## The file

```toml
[workspace]
name = "cometax"
base_port = 8000

[[workspace.services]]
name = "billing"
kind = "api"
port = 8010
path = "billing"

[[workspace.services]]
name = "admin"
kind = "spa"
port = 8040
path = "admin"
frontend = "vue"
```

Commit it. It holds no secrets, and it is what makes port allocation and
gateway generation reproducible on someone else's machine.

## Commands

| Command | Does |
| --- | --- |
| `jfast workspace init <name>` | Create the file |
| `jfast workspace list [--json]` | Services, kinds, ports, API base URL |
| `jfast workspace gateway [--force]` | Generate or refresh the gateway |
| `jfast workspace env` | Rewrite every frontend's `.env` |

`--json` on `list` returns the whole workspace, including `needs_gateway` and
`api_base_url` — the shape an agent should read instead of parsing the TOML.

---

## Deploying the workspace

Each service still generates its own compose file from its own plugin graph:

```bash
cd billing && jfast deploy compose -o docker-compose.yml
```

A single compose file spanning the whole workspace, with the gateway in front
and one shared network, is not implemented yet — see PLAN.md phase 4. Until
then, run them side by side or write the top-level compose by hand; the
per-service files give you the service definitions to paste.
