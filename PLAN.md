# JFastFramework — Roadmap

Phases are ordered by dependency, not by ambition. Each one ends with something
usable; nothing is half-shipped into the next phase.

Legend: `[x]` done · `[~]` partial, gaps named · `[ ]` not started

---

## Phase 0 — Kernel (done)

The smallest thing that makes every later phase possible.

- [x] `create_app()` — plugin resolution, registration, lifespan orchestration
- [x] `JFastSettings` / `JFastConfig` — typed config from `jfast.toml` + env
- [x] `AppContext` — `provide` / `require` indirection between plugins
- [x] Plugin contract — `meta`, `Settings`, lifecycle hooks, `infra()`, `describe()`
- [x] Registry — entry-point discovery, allow/deny lists, dependency ordering,
      cycle detection, duplicate-provider detection
- [x] RFC 7807 error model with handlers for domain, HTTP, validation and
      unhandled exceptions
- [x] `/health`, `/ready`, `/info`
- [x] Test fixtures (`build_test_app`, `client_for`, `NullPlugin`)
- [x] Kernel test suite

**Exit criteria met:** a service is `create_app()` and a `jfast.toml`.

---

## Phase 1 — Built-in plugins (mostly done)

- [x] `observability` — JSON logging, request-id and tenant-id correlation,
      access log. Default-enabled, zero extra dependencies.
- [x] `metrics` — Prometheus RED metrics, route-template labels (no cardinality
      explosion from path parameters), `/metrics`.
- [x] `sentry` — off by default, `SecretStr` DSN.
- [x] `database` — async SQLAlchemy engine, session factory, request-scoped
      session dependency with commit/rollback.
- [x] `cache` — Redis facade plus raw client, non-critical health check.
- [x] `mongo` — Motor client and database handle, for document-shaped data.
- [x] `qdrant` — client, health check, container with HTTP and gRPC ports.
- [x] `web` — Jinja2, static files, HTMX partial rendering, HTML error
      fragments for HTMX requests.
- [x] `VectorStore` protocol with pgvector and Qdrant implementations; the
      `rag` plugin picks one from config and fails at startup, naming the
      missing plugin, when the choice does not match the enabled graph.
- [~] `rag` — pluggable store and embedder, ingest/search/delete router.
      **Gaps:** fixed-size chunking only (bad for code and tables); no
      reranking; no hybrid BM25 + vector search; `ensure_schema` runs DDL at
      startup instead of through Alembic.

### Remaining in this phase

- [ ] `auth` — JWT RS256 verification, JWKS fetch with rotation and cache,
      scope dependencies
- [ ] `worker` — Arq wrapper: task registry, retry policy, dead-letter queue
- [ ] `internal_client` — service-to-service HTTP with retry, exponential
      backoff, **circuit breaker**, `X-Request-ID` propagation
- [ ] `websockets` — connection manager with a Redis pub/sub backplane

---

## Phase 2 — Multi-tenancy (not started)

The pitch of every SaaS factory and the part everyone gets wrong late. Decide
the strategy before writing any of it, because changing it afterwards is a data
migration, not a refactor.

- [ ] Choose: column `tenant_id` + enforced filter · PostgreSQL row-level
      security · schema-per-tenant. **Leaning RLS** — the only option where
      forgetting a filter is not a data leak.
- [ ] Tenant resolution from JWT claim / header / subdomain
- [ ] Tenant context propagated to the DB session (`SET LOCAL app.tenant_id`)
- [ ] Per-tenant rate limiting and quotas
- [ ] Tests that a query without tenant context returns zero rows

**Trade-off to accept now:** `BaseRepository` filters by tenant, but a raw
`session.execute` bypasses it. Until RLS lands, tenant isolation is a
convention, not a guarantee. Do not sell it as a guarantee.

---

## Phase 3 — The generator (partially done)

- [x] Jinja2 templates as real files — linted, diffed, tested
- [x] `jfast new module <name>` — layered layout with tests and a README
- [x] `--layout screaming` — framework-free domain, one file per use case
- [x] `--ui htmx` — overlay composed onto either layout, not duplicated
- [x] `jfast new service <name> --kind api|web` — a whole service from zero
- [x] Separate Jinja environments so HTML templates keep their runtime `{{ }}`
- [x] Pluralised table names, overridable with `--table`
- [x] `.jfast-template` stamp recording framework version and context
- [ ] Alembic layout and a CI workflow in the generated service
- [ ] `jfast upgrade` — re-apply newer templates over an existing tree and show
      a diff. **This is what the CometaX generator never had**, and the reason
      generated services drift.
- [ ] `jfast new plugin <name>` — scaffold a third-party plugin package
- [ ] `--kind spa` — Vue 3 + Vite project as a sibling service, with its own
      Dockerfile and a compose entry. Deliberately not shipped untested.
- [ ] RAG improvements: structure-aware chunking, reranking, hybrid search

---

## Phase 4 — Deployment (partially done)

- [x] `Plugin.infra()` — plugins declare their containers
- [x] `jfast deploy compose` — docker-compose derived from the plugin graph
- [x] `jfast deploy dockerfile` — non-root, healthcheck, layer-cached
- [x] `extra_ports` for multi-port containers (Qdrant HTTP + gRPC)
- [ ] `jfast deploy k8s` — Deployment, Service, ConfigMap, Secret, HPA
- [ ] `jfast deploy env` — `.env.example` derived from every plugin's settings
      schema, so config docs cannot go stale
- [ ] Port allocation registry — the CometaX 10-port-block scheme without a
      central IAM as a hard dependency
- [ ] GitHub Actions workflow template

---

## Phase 5 — Agent surface (partially done)

What makes "describe an app and get one" real rather than a demo.

- [x] `jfast describe --json` — settings schema, plugin graph, providers, infra
- [x] `jfast doctor` — config resolves, enabled plugins import
- [x] `.jfast/skills/` layout with four starter skills
- [x] `AGENTS.md` contract
- [ ] `jfast routes --json` — every mounted route with its schema
- [ ] `jfast skills list` — enumerate skills so an agent can choose one
- [ ] `DESIGN.md` convention wired into the frontend templates
- [ ] Recipe skills: `add-auth`, `add-worker`, `add-websocket`, `add-frontend`

---

## Phase 6 — Hardening (started)

- [x] `mypy --strict` clean across `src/`
- [x] `ruff check` + `ruff format --check` clean
- [x] CI: lint, format, types, tests and a scaffold smoke test on 3.11–3.13
- [ ] 90% test coverage on the kernel
- [ ] Integration suite against real PostgreSQL and Redis
- [ ] Benchmark: kernel overhead per request vs bare FastAPI (publish the number)
- [ ] Semantic versioning policy and a deprecation window
- [ ] Documentation site

---

## Explicit non-goals

Saying no keeps the kernel small.

- **Not an ORM.** SQLAlchemy is already good.
- **Not a control plane.** JFast services can register with an IAM, but the
  framework must run standalone. The CometaX `ApiIam` coupling is exactly what
  made the previous generation hard to reuse.
- **Not a frontend framework.** Templates for Vue 3, opinions about design
  tokens, nothing more.
- **Not multi-language.** Python only. The PHP/Laravel half of CometaX is a
  separate problem with a separate solution.

---

## Migrating from CometaXMicroservices

1. Pick one generated service — `FrameworkTest` is the natural first victim.
2. Replace its copied `core/` and `config/` with `jfastframework` imports.
3. Move every `os.getenv` into a plugin settings model.
4. Convert each `modules/<name>/` to the JFast module layout.
5. Delete the copied files and pin `jfastframework~=0.1`.
6. Only then migrate the second service.

Do not migrate all of them at once. The first migration is where the framework's
missing pieces surface, and you want to find them with one service at risk.
