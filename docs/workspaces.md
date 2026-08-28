# Workspaces and the API gateway

A single service needs no workspace. The moment there are two, three questions
appear that per-service config cannot answer:

* which ports are already taken;
* what the frontend should call;
* whether anything sits in front of them.

`jfast.workspace.toml` answers all three, and it is the only file that knows
the whole system.

---

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
