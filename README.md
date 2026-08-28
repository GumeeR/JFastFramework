# JFastFramework

**A plugin-based FastAPI framework for microservices, built to be driven by AI agents.**

Status: `0.1.0a1` — pre-alpha, unpublished. The version was reset from `0.7.0`
deliberately; [CHANGELOG.md](CHANGELOG.md#renumbering) says why. Maturity is per
subsystem rather than global: [STATUS.md](STATUS.md) lists what is trustworthy,
what is unverified, and what is known broken. [PLAN-NEXT.md](PLAN-NEXT.md) is the
road to 1.0; [PLAN.md](PLAN.md) tracks what is not done.

---

## One command

```bash
pip install jfastframework      # not on PyPI yet — see docs/local-setup.md
jfast start shop
```

A modular monolith in Python with PostgreSQL + pgvector, Redis and background
jobs; a Vue 3 frontend; a Caddyfile; a compose file. All of it wired together —
the frontend's API URL, the migration DSN, the container ports — rather than
four folders that happen to be adjacent.

A monolith and not three services, on purpose: you do not know the seams yet.
Splitting later is a move; un-splitting is a rewrite. When a module outgrows
it, `jfast new service` promotes it.

Prefer to choose? `jfast init` asks. Prefer flags? Every choice is one.

---

## What problem this solves

Scaffolding generators copy code. Every generated service becomes a fork frozen
at generation time: fix a bug in the shared HTTP client and you patch it in
twelve places by hand.

JFast splits the two concerns that generators conflate:

| | |
| --- | --- |
| **Runtime** (`jfastframework`) | A versioned library your services **import**. Fix it once, bump the pin. |
| **Generator** (`jfast` CLI) | Emits only the code that is genuinely yours. |

Everything above the kernel is a plugin.

```toml
[plugins]
enabled = ["observability", "metrics", "database", "cache", "queue"]
disabled = ["sentry"]
```

Delete `"cache"` and the Redis client, its health check and its container in
the generated compose file all disappear together. Infrastructure is derived
from the plugin graph, so it cannot drift from what the app actually loads.

## Plugins

| Plugin | Does | Extra |
| --- | --- | --- |
| `observability` | JSON logs, request-id and tenant correlation | — |
| `metrics` | Prometheus RED metrics, `/metrics` | `metrics` |
| `database` | Async SQLAlchemy, sessions, Alembic wiring | `db` |
| `cache` | Redis cache, pub/sub | `cache` |
| `queue` | Background jobs on PostgreSQL, Redis or RabbitMQ | `queue` |
| `events` | Kafka publish/subscribe | `kafka` |
| `mongo` | MongoDB via Motor | `mongo` |
| `qdrant` | Qdrant vector database | `qdrant` |
| `rag` | Retrieval over pgvector or Qdrant | `rag` |
| `web` | Jinja2 + HTMX partial rendering | `web` |
| `gateway` | Prefix-based reverse proxy | `gateway` |
| `auth` | JWT verification, scopes, revocation, social login | `auth` |
| `storage` | Files on local disks, S3 or MinIO | `storage` |
| `tenancy` | Tenant from a token claim, subdomain or path | — |
| `notifications` | Push over Firebase Cloud Messaging | `fcm` |
| `sentry` | Error and performance reporting | `sentry` |

Third-party plugins register through the same entry-point group, so nothing
here is privileged.

---

## Not only Python

A JFast service is not "a service written with JFast" — it is a service that
satisfies [the contract](docs/service-contract.md): `/health`, `/ready`,
`X-Request-ID`, `problem+json`, `JFAST_*` config, a ten-port block, JSON logs
on stdout.

```bash
jfast new service edge --language go --grpc
```

That Go service has **zero third-party dependencies** and implements the
contract in ~300 vendored lines. The gateway routes to it without knowing it is
Go; the workspace allocates its ports; Caddy fronts it alongside everything
else — because they all talk to the contract, not to the language.

CI runs `go vet`, `go test`, `go build`, starts the binary and curls it. A
scaffold nobody has run is a liability that looks like a feature.

`--grpc` generates the `.proto` contract. It does **not** generate stubs or
wire a server — see [proto/README.md](src/jfastframework/templates/proto/proto/README.md.j2)
for why, and for the commands to do it yourself.

---

## Queues and events

Two different things, two plugins:

```toml
[plugin.queue]
backend = "postgres"    # or "redis", "rabbitmq"
```

```python
@tasks.task("send_invoice_email")
async def send_invoice_email(payload: dict) -> None: ...

await queue.enqueue(Job(task="send_invoice_email", payload={"id": 7}))
```

Start with PostgreSQL: enqueueing shares the transaction that produced the
work, so a rollback takes the job with it. Redis buys latency, RabbitMQ buys
routing. [Which to pick, and why](docs/queues-and-events.md).

Delivery is at-least-once — handlers must be idempotent. Retries are bounded
and backoff is capped; exhausted jobs are dead-lettered rather than looping.

Events are the other half: `events` is Kafka, for "this happened" rather than
"do this".

---

## Frontends

**Server-rendered**, no build step:

```bash
jfast new service storefront --kind web
jfast new module product --ui htmx
```

**SPA**, Vue 3 or React with Vite and Tailwind v4:

```bash
jfast new service admin --kind spa --frontend vue
cd admin && jfast new view Facturas
```

`jfast new view` creates `src/ModuloFacturas/{Components,Pages,Routes,Services}`
and registers it in the router and the sidebar at their marker comments —
idempotently, failing loudly if a marker is gone.

Both frontends are installed and built in CI. That job exists because of a real
bug: the marker comment sat inside a block comment, whose inner `*/` closed it
early and left the router syntactically invalid. Every grep passed. Only
`vite build` caught it.

Angular and React Native are **not** generated. [Why](docs/frontend.md#angular).

---

## Workspaces, gateway, Caddy

```bash
jfast workspace init cometax
jfast new service billing --with database,cache
jfast new service catalog --with qdrant,rag     # a gateway appears here
jfast workspace compose && jfast workspace caddy
```

Services register themselves and take the next free ten-port block. At the
**second** backend a gateway is generated automatically — one backend
deliberately does not get one, because it would add a hop and an outage surface
for nothing.

Caddy is the edge (TLS, HTTP/3, compression, the built SPA); the gateway is the
application proxy behind it. Backends live under `/api` either way, so the
frontend's production build survives a gateway appearing.

[docs/workspaces.md](docs/workspaces.md) · [docs/deploy.md](docs/deploy.md)

---

## Migrations and tests, wired

Every generated service ships `alembic.ini`, `migrations/env.py`, `pytest.ini`
and `conftest.py`. `env.py` reads the same `JFAST_DB_DSN` the app does — a
migration cannot run against a different database than the service — and
imports every module's models automatically, so autogenerate never silently
emits an empty migration. [Details and the traps](docs/migrations-and-tests.md).

---

## Authentication

```toml
[plugin.auth]
mode = "jwks"                    # jwks | public_key | secret
jwks_url = "https://id.example.com/.well-known/jwks.json"
issuer = "https://id.example.com/"
audience = "billing"
algorithms = ["RS256"]
```

```python
@router.post("/invoices")
async def create(caller: Principal = Depends(require_scopes("invoices:write"))):
    ...
```

Verification, scopes and roles, JWKS key rotation, refresh rotation with reuse
detection, and revocation shared across replicas through Redis.

The defaults refuse the attacks that do not look like failures: `alg: none`,
RS256→HS256 confusion (configuring both algorithm families at once is rejected
at startup — that combination *is* the attack), tokens minted for a sibling
service, and a generous clock skew. Rejection reasons go to the log; the client
gets a plain 401.

**Tenancy stops being forgeable.** Without auth, `tenant_id` comes from the
`X-Tenant-ID` header — settable by anyone with curl. With it, from a signed
claim.

There is no `/auth/login`: checking a password against your user table is your
application's job. `auth.issuer` is provided for your own route.
[docs/auth.md](docs/auth.md).

## Kubernetes

```bash
jfast workspace k8s --host app.example.com
kubectl apply -k k8s/overlays/dev
```

`jfast init` asks whether you need it. You get a kustomize tree: Deployment,
Service, ConfigMap, HPA and PodDisruptionBudget per service, one Ingress
serving `/api` — the same public shape as the generated Caddyfile — and
`dev`/`prod` overlays.

Liveness probes `/health`, readiness probes `/ready`. That two-endpoint
contract is what stops a database blip from restarting every healthy pod at
once.

**Databases are not generated.** A StatefulSet for PostgreSQL from a scaffolder
is how people lose data. The manifests read a DSN from a Secret.
[docs/kubernetes.md](docs/kubernetes.md).

## Contracts: rules an agent cannot drift past

`AGENTS.md` says what to do. A **contract** says what is allowed, and something
checks it — which is the difference between a rule and a suggestion.

Every generated service ships a `contracts.toml` you own:

```toml
[project]
owns = "Invoices and payments."
does_not_own = "Customers. Ask the catalog service."

[layers.domain]
paths = ["modules/*/[!_]*.py"]
may_import = []
forbid_packages = ["fastapi", "sqlalchemy", "pydantic"]

[[rules.forbid_call]]
pattern = "os.getenv"
except_in = ["settings.py"]
why = "Configuration is typed. Add a field to a settings model."
```

```bash
jfast contracts check
```

```
modules/invoice/repository.py:1: layer-package: 'storage' must not import 'fastapi'
  (Data access. No business rules.)
```

Non-zero exit — in CI, a failed build. An agent generating code at speed drifts
past prose; it does not drift past a failing check.

Three audiences, one file: the build reads it through `check`, an agent through
`jfast contracts show --json`, a reviewer through the generated `CONTRACTS.md`.
Waivers are inline and require a reason. [docs/contracts.md](docs/contracts.md).

## Why AI agents are a first-class audience

```bash
jfast contracts show --json  # the rules THIS project holds itself to
jfast describe --json        # settings schema, plugin graph, providers, infra
jfast workspace list --json  # services, ports, API base URL, needs_gateway
jfast doctor
```

No grepping. Plus `.jfast/skills/` — one folder per task with a `SKILL.md`
stating when to use it and the exact steps — and [AGENTS.md](AGENTS.md), the
rules an agent must follow here.

---

## Documentation

The site is built from these same files: **<https://jfabrizzio5.github.io/JFastFramework/>**

| Document | Contents |
| --- | --- |
| [docs/local-setup.md](docs/local-setup.md) | Installing from a checkout and making your first project |
| [docs/contracts.md](docs/contracts.md) | Per-project rules, enforced |
| [docs/auth.md](docs/auth.md) | JWT: modes, the attacks refused, revocation, Google login |
| [docs/storage.md](docs/storage.md) | Disks, signed URLs, S3 and MinIO |
| [docs/multitenancy.md](docs/multitenancy.md) | Subdomains, trust order, what it is not |
| [docs/cloud.md](docs/cloud.md) | Secret managers, serverless functions, push |
| [docs/kubernetes.md](docs/kubernetes.md) | Manifests, probes, what is not generated |
| [docs/service-contract.md](docs/service-contract.md) | What every service must do, in any language |
| [docs/modules.md](docs/modules.md) | Module layouts, HTMX, service kinds |
| [docs/datastores.md](docs/datastores.md) | PostgreSQL, Redis, Mongo, Qdrant |
| [docs/queues-and-events.md](docs/queues-and-events.md) | Jobs, streams, backends |
| [docs/frontend.md](docs/frontend.md) | HTMX, Vue, React, the view generator |
| [docs/workspaces.md](docs/workspaces.md) | Many services, the gateway |
| [docs/migrations-and-tests.md](docs/migrations-and-tests.md) | Alembic, pytest |
| [docs/plugins.md](docs/plugins.md) | Writing a plugin |
| [docs/deploy.md](docs/deploy.md) | Compose, Caddy, Dockerfile |
| [docs/skills.md](docs/skills.md) | Writing a skill |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Decisions and their costs |
| [PLAN.md](PLAN.md) | Done, partial, not started |

## Verify

```bash
pytest                             # 326 framework tests
ruff check src tests docs-site && ruff format --check src tests docs-site
mypy src                           # strict

bash scripts/smoke.sh              # both module layouts, HTMX, alembic, a booting service
bash scripts/smoke_contracts.sh    # a generated service passes its own contract
bash scripts/smoke_auth_k8s.sh     # auth guards routes; manifests parse
bash scripts/smoke_workspace.sh    # workspace, gateway, view patching
bash scripts/smoke_start.sh        # the default stack, end to end
bash scripts/smoke_docs.sh         # the quickstart, run exactly as written
bash scripts/smoke_go.sh           # go vet, test, build, run, curl   (needs go)
bash scripts/smoke_frontend.sh     # npm install + vite build         (needs node)
python docs-site/build.py --version latest --output site/latest
python docs-site/check.py site/latest
```

## What is not verified

Said plainly, because a framework that overstates its coverage is worse than
one that admits the gap:

- **RabbitMQ and Kafka** are written against documented APIs but never
  round-tripped against real brokers in CI.
- **Multi-tenancy** is a convention enforced by `BaseRepository`, not an
  isolation guarantee. Row-level security is phase 2.
- **RAG** chunks at fixed width with no reranking.
- **Angular, React Native, Laravel, .NET** are not generated at all.

## License

MIT.
