# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/). Services pin `jfastframework~=0.6`.

## [Unreleased]

## [0.6.0] - 2026-08-27

JWT authentication, and Kubernetes manifests derived from the service contract.

### Added

**`auth` plugin**
- Verification in three modes: `jwks` (fetch the issuer's public keys — the
  default, and the only sane one across services), `public_key` (a pinned PEM),
  `secret` (HMAC, for a single service).
- `require_auth`, `require_scopes(...)`, `require_roles(...)`, `optional_auth`
  as FastAPI dependencies. 401 for "who are you", 403 for "you may not".
- JWKS client with caching, rotation on an unknown `kid`, and a rate limit on
  refresh so forged `kid`s cannot be used to hammer the identity provider.
  Cached keys keep working through a JWKS outage; `/ready` reports staleness.
- Token issuance for a service that owns its own login, with **refresh
  rotation and reuse detection**: a replayed refresh token revokes the whole
  session family.
- Revocation: `POST /auth/logout` denies the `jti` and its refresh family,
  backed by Redis when the `cache` plugin is on. The in-memory fallback
  reports itself as not shared rather than pretending.
- `GET /auth/me` returns identity and permissions — never the token, never the
  raw claims.

**Security decisions, each with a test**
- Algorithms are pinned by configuration and passed explicitly to the decoder,
  so `alg: none` and RS256→HS256 confusion are both refused. Configuring
  symmetric and asymmetric algorithms together is rejected at startup: that
  combination *is* the attack.
- `aud` and `iss` are verified — off by default in most libraries, and without
  them a token for a sibling service is accepted here.
- Expiry leeway is 30 seconds, not minutes.
- Rejection reasons go to the log; the client gets a plain 401.
- **`tenant_id` now comes from a signed claim**, not the forgeable
  `X-Tenant-ID` header. This is the main security reason to enable auth.

**Kubernetes**
- `jfast workspace k8s` — a kustomize tree: Deployment, Service, ConfigMap,
  HPA and PodDisruptionBudget per service, one Ingress, `dev`/`prod` overlays.
- `jfast init` asks whether you need it.
- Liveness probes `/health`, readiness probes `/ready` — the two-endpoint
  contract is what keeps a database blip from restarting every healthy pod.
  A startup probe allows 150s for a slow first boot.
- Non-root, read-only root filesystem, dropped capabilities,
  `maxUnavailable: 0`, and a PDB so a node drain cannot take every replica.
- Ingress serves `/api`, the same shape as the generated Caddyfile, so the
  frontend build is identical locally and in the cluster.

### Not generated, deliberately
- **Databases.** A StatefulSet for PostgreSQL from a scaffolder is how people
  lose data. The manifests read a DSN from a Secret.
- **Real secrets.** `*-secrets.example.yaml` holds placeholders.
- **A login endpoint.** Checking a password against your user table is the
  application's job; `auth.issuer` is provided for your own route.
- **NetworkPolicies, ServiceMonitors, migration Jobs, Helm.** Each needs a
  decision about your system that a generator should not guess.

### Notes
- The manifests are validated as YAML and asserted structurally in
  `tests/test_kubernetes.py`. They have **not** been applied to a real
  cluster in CI. Treat the first `kubectl apply` as the test.

## [0.5.0] - 2026-08-27

Per-project contracts, and a quickstart that CI actually executes.

### Added

**Contracts**
- `contracts.toml` in every generated service: scope (`owns` /
  `does_not_own`), layer boundaries, forbidden calls, required structure,
  declared interfaces, and invariants no checker can verify.
- `jfast contracts init | check | show --json | render | waivers`.
  `check` exits non-zero, so it fails a build rather than printing advice.
- A static, AST-based checker: layer boundaries (relative and absolute
  imports), per-layer forbidden packages, forbidden calls with the reason
  attached, and required files per module.
- Inline waivers — `# contracts: allow <reason>` — with the reason required
  and `jfast contracts waivers` listing every one.
- The contract is validated before the code: two layers claiming one path, or
  a `may_import` naming a layer that does not exist, are reported as contract
  errors rather than producing confident answers to the wrong question.
- `CONTRACTS.md` generated from the same file, so the document and the
  enforced rule cannot disagree.
- `.jfast/skills/respect-contracts/SKILL.md`, and `AGENTS.md` now opens with
  `jfast contracts show --json`.

**Getting started**
- `docs/local-setup.md` — installing from a checkout, generating a project,
  the loop you actually use, and the failure modes worth knowing.
- `scripts/smoke_docs.sh` runs those commands **exactly as documented**, in
  CI. Documentation that has never been executed is a guess.

### Fixed
- **A generated service with `queue` enabled could not start without
  PostgreSQL.** `setup()` raised at startup, so the process crash-looped with
  an asyncpg traceback instead of serving. It now starts, logs the reason, and
  reports itself unready — an orchestrator handles "not ready" gracefully and
  handles a crash loop by paging someone. Same fix for `rag`'s `auto_migrate`.
- The layered contract defaults claimed `modules/*/schemas.py` for two layers.
  Caught by a freshly generated service failing its own contract, which is
  exactly the check that should catch it.
- Layer matching ranked patterns by string length, so the screaming layout's
  catch-all `modules/*/[!_]*.py` beat `modules/*/http.py` and classified every
  router as domain code. It now ranks by specificity — fewest wildcards.

## [0.4.0] - 2026-08-27

Polyglot services, queues and events, one-command start, Caddy at the edge,
and a documentation site — plus real build verification for everything that
had until now only been verified by grep.

### Added

**`jfast start`**
- One command for the opinionated default: a Python modular monolith with
  PostgreSQL + pgvector, Redis, background jobs and a starter module, a Vue
  frontend, a Caddyfile and a workspace compose file.
- A monolith rather than three services on purpose: splitting later is a move,
  un-splitting is a rewrite.

**Polyglot services**
- `docs/service-contract.md` — the contract every JFast service satisfies
  regardless of language: `/health`, `/ready`, `X-Request-ID`, problem+json,
  `JFAST_*` config, ten-port blocks, JSON logs on stdout.
- `jfast new service --language go` — a Go service with **zero third-party
  dependencies**, implementing the contract in ~300 vendored lines. CI runs
  `go vet`, `go test`, `go build`, starts the binary and curls it.
- `jfastframework/languages.py` — the language registry. You only need the
  toolchain for the languages you actually use.

**gRPC**
- `--grpc` generates the `.proto` contract (health, problem, a domain service)
  and reserves port offset +9. **Contract only:** no stubs are generated and no
  server is wired, because pinning a `protoc` version inside a scaffolder makes
  generated stubs disagree with whatever CI has. See `proto/README.md`.

**Queues**
- `queue` plugin with a `QueueBackend` protocol and three backends: PostgreSQL
  (`FOR UPDATE SKIP LOCKED`, transactional enqueue), Redis (`BLMOVE` into a
  per-worker processing list), RabbitMQ (dead-letter exchange with a TTL for
  delays).
- `TaskRegistry` and `Worker`: bounded exponential backoff, dead-lettering,
  job timeouts, in-flight draining on shutdown, immediate wake on stop.
- `GET /queue/stats`.

**Events**
- `events` plugin — Kafka publish/subscribe with partition keys, offsets
  committed after handling, and KRaft-mode infrastructure (no ZooKeeper).

**Edge and workspace deployment**
- `jfast workspace compose` — one compose file for every service, whatever its
  language, with per-service datastores.
- `jfast workspace caddy` — a Caddyfile putting the workspace behind one
  hostname. Backends live under `/api` with or without a gateway, so the
  frontend's production build keeps working the day one appears.
- Frontends gained `.env.production` with `VITE_API_URL=/api` — relative, so
  no CORS and no rebuild per environment.

**Documentation site**
- `docs-site/build.py` renders the repository's own markdown into a static,
  versioned site; `docs-site/check.py` validates links, anchors, assets, theme
  tokens and unrendered template artifacts.
- `.github/workflows/pages.yml` publishes it, rebuilding every released minor
  version from its own tag so older versions keep working.

### Fixed
- **The generated Vue and React routers were not valid JavaScript.** The marker
  comment `/*nuevaRuta*/` sat inside a `/* … */` block comment, whose inner
  `*/` closed the comment early. Every grep-based check passed — the marker
  *was* there — and only `vite build` caught it. Both frontends now install and
  build in CI.
- **The worker busy-waited on any non-blocking backend.** PostgreSQL polls and
  returns instantly when the queue is empty, so the loop never yielded: it
  burned a core and starved the event loop, which meant the HTTP handlers in
  the same process stopped responding while "the worker is running".
- `jfast start`'s frontend called a dev port that Caddy served under a
  different path. Both now agree on `/api`.

### Changed
- `PLUGIN_CATALOG` gained `queue` and `events`; `--with queue` pulls in a
  backend the service can actually reach, the same way `rag` does.
- `SERVICE_KINDS` and the installer offer Go.
- CI gained three jobs: Go (`setup-go`), frontend (`setup-node`) and the docs
  site. The frontend job exists because of the router bug above.

### Not done, deliberately
- **Angular** is not generated. A hand-rolled `angular.json` that has never run
  under `ng serve` looks finished and fails in a way that is hard to attribute.
- **React Native** is not generated, for the same reason.
- **RabbitMQ and Kafka** are written against documented APIs but have not been
  round-tripped against real brokers in CI.
- **Laravel and .NET** extensions are not started. The service contract is the
  extension point; a language needs a `LanguageSpec`, a template tree and a CI
  job that builds what it generates.

## [0.3.0] - 2026-08-27

Multi-service workspaces, an API gateway, frontend generation, and the
migration/testing setup that the previous release only *documented*.

### Added

**Workspaces**
- `jfast.workspace.toml` and `jfastframework.workspace`: services register
  themselves, take the next free ten-port block, and the file records what the
  frontend should call.
- `jfast workspace init | list | gateway | env`.

**API gateway**
- `gateway` plugin: prefix-based reverse proxy with hop-by-hop header
  stripping, `X-Request-ID` propagation, and 502/504 as problem+json.
- Generated automatically once a workspace has more than one backend. One
  backend deliberately does not get one.
- Not a catch-all: only configured prefixes are proxied, so the gateway keeps
  its own `/health`, `/ready` and `/metrics`. Readiness does not probe
  upstreams, so one restart does not fail the whole system.

**Frontends**
- `jfast new service <name> --kind spa --frontend vue|react` — Vite +
  Tailwind v4 project with a working home page that calls the backend's
  `/health` on load.
- `jfast new view <Name>` — the `Modulo<Name>/{Components,Pages,Routes,Services}`
  structure, registered in the router and the sidebar at marker comments.
- `jfastframework.cli.patcher`: idempotent, loud, marker-preserving patching.
  Re-running the generator does not duplicate; a missing marker raises with the
  path instead of silently doing nothing.
- Framework auto-detected from the project, so `--frontend` is not repeated.

**Migrations and tests in generated services**
- `alembic.ini`, `migrations/env.py`, `script.py.mako` and `versions/`.
  `env.py` reads the app's own `JFAST_DB_DSN` and auto-imports every module's
  models, so autogenerate cannot silently emit an empty migration.
  `compare_type` and `compare_server_default` are on.
- `pytest.ini` and a `conftest.py` with `app` / `client` fixtures.

**Datastore selection from the terminal**
- `jfast init` — interactive installer: kind, frontend, datastores, port.
- `jfast new service --with database,cache,qdrant,rag` — the plugin list, the
  `[plugin.*]` blocks, the `.env` keys and the pinned extras all derive from it.
- `rag`'s store is inferred from the datastores chosen, so `--with qdrant,rag`
  cannot generate a service configured for pgvector.

### Changed
- **Breaking:** `jfast new service --port` now defaults to the next free block
  in the workspace instead of 8000.
- `SERVICE_KINDS` gained `spa` and `gateway`.
- `.vue`, `.jsx` and `.tsx` templates render through the square-bracket Jinja
  environment, so Vue interpolation and JSX braces survive scaffolding.

### Fixed
- **CI:** `mypy --strict` failed on `redis.asyncio.from_url` being untyped in
  some redis releases and annotated in others — a strict run that passed
  locally and failed in CI on nothing we wrote. Optional third-party packages
  are now `follow_imports = "skip"`, which is honest about types we neither
  control nor can rely on across the support matrix.
- **CI:** `scripts/smoke.sh` hardcoded `.venv/bin/python`, which does not exist
  in a CI job. It now falls back to `PATH`.
- Dropped the dead `tomli` dependency marker (`requires-python` is already
  `>=3.11`).

## [0.2.0] - 2026-08-27

Datastores became a choice, and a frontend became a service.

### Added

**Datastores**
- `VectorStore` protocol in `jfastframework.vectors`, with `Chunk` and
  `SearchHit` as the shared vocabulary. Every store normalises its score to
  cosine similarity in [0, 1].
- `qdrant` plugin — client, health check, container with HTTP and gRPC ports.
- `mongo` plugin — Motor client and database handle.
- `rag` now selects its store from config: `pgvector`, `qdrant`, or a dotted
  path to your own class. Same for the embedder.
- `InfraService.extra_ports` for containers exposing more than one port.

**Server-rendered frontends**
- `web` plugin — Jinja2 templates, static files, and `render()` with HTMX
  partial rendering: a browser navigation gets the page, an `hx-get` gets the
  fragment, from one handler.
- HTMX-aware error handling: a `JFastError` raised during an HTMX request
  returns an HTML fragment instead of `problem+json`, which HTMX would
  otherwise swap into the DOM as raw text.

**Generator**
- `jfast new service <name> [--kind api|web]` — scaffolds a whole service.
- `jfast new module <name> [--layout layered|screaming] [--ui api|htmx]`.
- `module_screaming` layout: framework-free domain, one file per use case,
  storage and HTTP at the edges, domain tests separated from use-case tests.
- `ui_htmx` overlay — composed onto either layout rather than duplicated, so
  three template trees cover all four combinations.
- Two Jinja environments in the scaffolder: `.html.j2` templates use `[[ ]]`
  for scaffold-time values so the runtime `{{ }}` the browser needs survives.
- Table names are pluralised, which also dodges the SQL reserved words that
  singular nouns keep landing on (`order`, `user`, `group`). Override with
  `--table`.

**Docs**
- `docs/modules.md`, `docs/datastores.md`.

### Changed
- **Breaking:** `jfast new <name>` is now `jfast new module <name>`.
- **Breaking:** `[plugin.rag] table` renamed to `collection` — it names a
  Qdrant collection just as often as a PostgreSQL table now.
- `rag` no longer hard-requires `database`. It declares `after` and validates
  the store it was actually configured with, naming the missing plugin.
- Module templates moved to `module_layered/`; both layouts now export
  `build_service(session, tenant_id)`, the seam the HTMX overlay consumes.
- mypy no longer pins `python_version`; it checks against the interpreter it
  runs on, which CI varies across the support matrix.

### Fixed
- `chunk_text` emitted a final sliver already contained in the previous chunk
  whenever the text did not divide evenly — a wasted embedding call and a
  duplicate in every result set.

## [0.1.0] - 2026-08-27

First alpha. Kernel and built-in plugins.

### Added
- `create_app()` with plugin resolution, registration and lifespan orchestration
- Typed configuration: `JFastSettings`, `JFastConfig`, `jfast.toml` + env
- `AppContext` with `provide` / `require` indirection between plugins
- Plugin contract: `PluginMeta`, `PluginSettings`, lifecycle hooks,
  `infra()`, `describe()`
- Registry: entry-point discovery, allow/deny lists, dependency ordering,
  cycle detection, duplicate-provider detection
- RFC 7807 `application/problem+json` error model
- `/health`, `/ready`, `/info` system endpoints
- Built-in plugins: `observability`, `metrics`, `sentry`, `database`, `cache`, `rag`
- `jfastframework.db`: declarative `Base` with a pinned constraint naming
  convention, `TimestampMixin`, `TenantMixin`, generic `BaseRepository`
- Deploy generation: `docker-compose` and `Dockerfile` derived from the plugin
  graph's `infra()` declarations
- `jfast` CLI: `new`, `describe`, `doctor`, `plugins list`, `deploy`
- Jinja2 module template, `jfastframework.testing` fixtures
- Agent surface: `AGENTS.md`, `.jfast/skills/` with four starter skills

### Notes
- Multi-tenancy is a convention enforced by `BaseRepository`, not a guarantee.
  Row-level security is phase 2. Do not describe it as isolation until then.
- The v0 prototype is preserved under `legacy/` for reference.
