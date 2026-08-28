# Subsystem maturity

One version number cannot describe this repository honestly. The kernel is
tested, typed and exercised by CI on every push; the Kafka client has never
spoken to a broker. Calling both "0.7.0" told a reader nothing, which is why
the package restarts at `0.1.0a1` and why maturity is tracked here, per
subsystem, instead.

## Levels

| Level | Means |
| --- | --- |
| `beta` | Exercised by CI against the real thing. API may still change, behaviour is known. |
| `alpha` | Tested in isolation, used by the author, not proven against production traffic. |
| `experimental` | Shipped to get feedback. Expect the API to move. |
| `unverified` | Written against documented APIs. **Never executed against the real dependency.** |
| `broken` | A known defect, named below. Do not build on it yet. |

**Promotion gate:** a subsystem does not move up a level because it feels more
finished. It moves when a CI job exercises it against the real dependency —
a container, a broker, a cluster. That is the same rule that let the Go
scaffold ship and kept the Angular one out.

## Kernel

| Subsystem | Level | Notes |
| --- | --- | --- |
| `create_app`, plugin registry, `AppContext` | `beta` | Full test suite, `mypy --strict`, entry-point discovery and cycle detection covered. |
| Settings and `jfast.toml` loading | `beta` | Typed, env-overridable. CORS, trusted hosts, body-size limit and request timeout are settings; all off unless configured. |
| RFC 7807 error model | `beta` | Handlers for domain, HTTP, validation and unhandled errors. |
| `/health`, `/ready`, `/info` | `beta` | Readiness runs every check concurrently under a timeout, and reports `timeout` apart from `fail`. `/docs` and `/openapi.json` close in production with `/info`. |
| Test fixtures | `beta` | `build_test_app`, `client_for`, `NullPlugin`. |

## Plugins

| Plugin | Level | Notes |
| --- | --- | --- |
| `observability` | `beta` | JSON logs, request-id and tenant correlation. Zero dependencies. |
| `metrics` | `beta` | Route-template labels, so path parameters cannot explode cardinality. |
| `contracts` (checker) | `beta` | Runs in CI against a generated service; a violation fails the build. Layers, forbidden calls and imports, required structure, and event-loop blocking. |
| `database` | `alpha` | Engine and session wiring, ordered pagination, and a tenant filter that raises rather than failing open. Repository behaviour is covered against SQLite; not run against PostgreSQL in CI. |
| `cache` | `alpha` | Redis facade and health check. Not run against a real Redis in CI. |
| `auth` | `alpha` | Verification, JWKS rotation, refresh reuse detection and the refused-attack defaults are tested. No PKCE, no mTLS, no real identity provider in CI. |
| `tenancy` | `alpha` | Resolution order is correct and tested. This is a **convention**, not isolation: a raw `session.execute` bypasses it. Row-level security is not implemented. |
| `storage` (local disk) | `alpha` | Key validation, symlink check, atomic writes, signed URLs — all tested. |
| `storage` (S3 / MinIO) | `unverified` | Never run against a real S3 or MinIO endpoint. |
| `web` (Jinja + HTMX) | `alpha` | Rendered and smoke-tested; no browser-level test. |
| `gateway` | `alpha` | Prefix routing, header stripping, problem+json errors. One upstream per prefix: no pool, no load balancing, no rate limiting. |
| `queue` (PostgreSQL) | `alpha` | `FOR UPDATE SKIP LOCKED`, correct reclaim of jobs from a dead worker. Not run against a real PostgreSQL in CI. |
| `queue` (Redis) | `alpha` | Visibility timeout implemented: workers heartbeat into a registry on server time and any worker reclaims a dead peer's in-flight jobs. Tested against an in-memory double of the Redis commands, not yet against a real server. |
| `queue` (RabbitMQ) | `unverified` | Never run against a broker. |
| `events` (Kafka) | `unverified` | Never run against a broker. No outbox, so a committed row can lose its event. |
| `mongo` | `alpha` | Client and handle only. No document contract, no migrations, no repository parity with SQL. |
| `qdrant` | `alpha` | Client, health check, container. |
| `rag` | `experimental` | Fixed-width chunking, no reranking, no hybrid search, and `ensure_schema` runs DDL at startup instead of through Alembic. |
| `notifications` (FCM) | `unverified` | Payload construction is tested; a delivery has never been made from CI. |
| `sentry` | `alpha` | Off by default. |

## Generator and deployment

| Area | Level | Notes |
| --- | --- | --- |
| `jfast new module` / `new service` (Python) | `beta` | Both layouts, the HTMX overlay, Alembic and pytest wiring are rendered and run in CI. |
| Vue and React scaffolds | `alpha` | `npm install` plus `vite build` run in CI, which is what caught the router marker bug. No runtime test. |
| Go service scaffold | `alpha` | `go vet`, `go test`, `go build`, then the binary is started and curled. |
| gRPC | `experimental` | The `.proto` contract is generated and the port reserved. No stubs, no server wiring. |
| Workspaces and port allocation | `beta` | Resources are named instances and services bind to them under a variable; the whole flow is exercised in CI. A 0.1 file still loads and `migrate-resources` converts it. |
| `deploy compose` / `dockerfile` | `alpha` | Derived from the plugin graph. One container per resource, with the connection strings generated beside them. |
| `workspace k8s` | `unverified` | Manifests are structurally asserted in tests. They have never been applied to a cluster, not even kind. |
| `deploy function` (Lambda / Cloud Run) | `unverified` | Writes scripts rather than running them; never applied against a real account. |
| `load_secrets` (AWS / GCP) | `unverified` | Never run against a real secret manager. |

## Not present at all

Named here so nobody has to grep to find out:

- **No service-to-service HTTP client.** No retries, no circuit breaker, no timeout policy. Every generated service writes its own.
- **No distributed tracing.** Logs and metrics only; `request_id` gives you grep, not spans.
- **No schema or module diagrams.** `jfast workspace graph` draws the services and resources; the database schema and the module import graph do not.
- **No declared use cases.** What a service does is not written down anywhere a build can check.
- **No rate limiting**, at the gateway or anywhere else.
- **No websockets or SSE.**
- **No scheduler.** Delayed jobs exist; recurring ones do not.
- **No row-level security.** See `tenancy` above.
- **No dependency lock and no upper bounds.** A FastAPI release can break this repository with no warning, and there is no record of which versions any given commit was tested against.
- **Published, but pre-alpha.** `pip install --pre jfastframework` resolves;
  without `--pre` it does not, which is the packaging tool stating the
  maturity on this page rather than a README asking you to believe it.

[PLAN-NEXT.md](PLAN-NEXT.md) is the ordered plan for closing all of it.
