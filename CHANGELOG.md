# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/). Services pin `jfastframework~=0.2`.

## [Unreleased]

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
