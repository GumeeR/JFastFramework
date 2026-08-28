# JFastFramework

**A plugin-based FastAPI framework for microservices, built to be driven by AI agents.**

Status: `0.2.0` — alpha. The kernel, the built-in plugins and the generator
work and are tested end to end; several subsystems in [PLAN.md](PLAN.md) are
not written yet. This README marks what is real and what is planned.

---

## What problem this solves

Scaffolding generators copy code. Every generated service becomes a fork frozen
at generation time: fix a bug in the shared HTTP client and you patch it in
twelve places by hand.

JFast splits the two concerns that generators conflate:

| | |
| --- | --- |
| **Runtime** (`jfastframework`) | A versioned library your services **import**. Fix it once, bump the pin, done. |
| **Generator** (`jfast` CLI) | Emits only the code that is genuinely yours — modules, templates, deploy files. |

Everything above the kernel is a plugin. Monitoring, databases, cache, vector
search, HTML rendering, error tracking — all removable, all replaceable, none
privileged.

```toml
# jfast.toml — this is the whole configuration surface
[app]
name = "billing"
port = 8010

[plugins]
enabled = ["observability", "metrics", "database", "cache", "web"]
disabled = ["sentry"]
```

Delete `"metrics"` and the Prometheus middleware, the `/metrics` endpoint and
the Prometheus container in the generated `docker-compose.yml` all disappear
together. Infrastructure is derived from the plugin graph, so it cannot drift
away from what the app actually loads.

---

## Install

```bash
pip install -e ".[all,dev]"
```

Extras are granular so a service carries only what it uses: `server`, `db`,
`cache`, `mongo`, `qdrant`, `rag`, `web`, `metrics`, `sentry`, `worker`.

## A service in five lines

```python
# main.py
from jfastframework import create_app

app = create_app()
```

You get, without writing any of it:

- `GET /health` — liveness, cheap, no dependency probing
- `GET /ready` — readiness, aggregates every plugin's health check
- `GET /info` — plugin inventory and providers (auto-disabled in production)
- `GET /metrics` — Prometheus RED metrics
- structured JSON logs with a request id propagated through `X-Request-ID`
- RFC 7807 `application/problem+json` for every error, including unhandled ones

Or generate the whole thing:

```bash
jfast new service billing --port 8010
```

---

## Built-in plugins

| Plugin | Does | Provides | Extra | On by default |
| --- | --- | --- | --- | --- |
| `observability` | JSON logs, request-id and tenant correlation | `logger` | — | yes |
| `metrics` | Prometheus RED metrics, `/metrics` | `metrics.registry` | `[metrics]` | yes |
| `database` | Async SQLAlchemy, request-scoped sessions | `db.engine`, `db.sessionmaker` | `[db]` | no |
| `cache` | Redis cache, pub/sub, queue | `cache`, `cache.client` | `[cache]` | no |
| `mongo` | MongoDB via Motor | `mongo.client`, `mongo.db` | `[mongo]` | no |
| `qdrant` | Qdrant vector database | `qdrant.client` | `[qdrant]` | no |
| `rag` | Retrieval over a pluggable store | `rag.store`, `rag.embedder` | `[rag]` | no |
| `web` | Jinja2, static files, HTMX partials | `templates`, `render` | `[web]` | no |
| `sentry` | Error and performance reporting | — | `[sentry]` | no |

Third-party plugins register through the same entry-point group, so nothing
here is privileged — a plugin you write can replace any of them.

---

## Choosing your datastores

Vector search is a config line, not a rewrite:

```toml
[plugin.rag]
store = "pgvector"   # the database you already run
# store = "qdrant"   # when pgvector stops being enough
# store = "myapp.stores:WeaviateStore"
```

Picking a store whose plugin is not enabled fails at startup with the fix in
the message, not at the first search in production. Full guidance, including
when Qdrant is actually worth its operational cost, in
[docs/datastores.md](docs/datastores.md).

---

## Frontend as a service, Laravel-shaped

A frontend is a service like any other — it logs, reports health, and deploys
identically. It just returns HTML.

```bash
jfast new service storefront --kind web --port 8020
cd storefront
jfast new module product --ui htmx
```

Server-rendered Jinja2 with HTMX. No JavaScript build step, no separate repo,
no API-and-SPA split until you actually want one.

The idea worth knowing is **partial rendering**:

```python
return render(request, "product/index.html", {"page": page},
              partial="product/_rows.html")
```

A browser navigation gets the whole page. An `hx-get` gets just the rows. One
handler, one context, no duplicated markup. Errors raised during an HTMX
request come back as HTML fragments rather than `problem+json`, because HTMX
swaps the body into the DOM and JSON would render as raw text.

---

## Module layouts

```bash
jfast new module invoice                      # layered
jfast new module invoice --layout screaming   # one file per use case
jfast new module invoice --ui htmx            # add server-rendered pages
```

**`layered`** — `router.py` / `service.py` / `repository.py` / `models.py` /
`schemas.py`. The familiar shape, right for CRUD.

**`screaming`** — the directory listing is the feature list:

```
modules/invoice/
├── invoice.py           the domain: entity + rules, framework-free
├── use_cases/
│   ├── create_invoice.py
│   ├── list_invoices.py
│   └── ...              one file per capability
├── storage.py           SQLAlchemy model + repository
├── http.py              router + schemas
└── tests/
    ├── test_invoice_domain.py      no database, no fakes, no event loop
    └── test_invoice_use_cases.py   fake repository, nothing else
```

Imports point inward only. Both layouts export the same
`build_service(session, tenant_id)`, which is what lets the HTMX overlay work
against either without knowing how the module is organised inside.

Details in [docs/modules.md](docs/modules.md).

---

## Generate deployment

```bash
jfast deploy compose --stdout
jfast deploy dockerfile
```

Compose is derived from the enabled plugin graph. Enable `qdrant` and the
container, its two ports and its volume appear; disable `cache` and Redis is
gone. The Dockerfile runs as a non-root user with a healthcheck on `/health`.

---

## Why AI agents are a first-class audience

Handing an agent a codebase usually means it greps around and guesses. JFast
gives it three things instead:

**1. Machine-readable state.** `jfast describe --json` returns the settings
schema, the resolved plugin graph, every provider key and every infra
container — without importing the app.

```bash
jfast describe --json | jq '.providers'
jfast plugins list --all
jfast doctor
```

**2. Skills.** `.jfast/skills/` holds one folder per task, each with a
`SKILL.md` stating when to use it and the exact steps, so the agent loads only
what it needs. See [.jfast/skills/README.md](.jfast/skills/README.md).

**3. A contract.** [AGENTS.md](AGENTS.md) states the rules an agent must
follow: layer boundaries, what may never be hardcoded, which commands verify a
change.

Drop a `DESIGN.md` next to a frontend and the agent has your visual language
too. The structure is knowable in advance, so "build me an app" lands in a
shape you already reviewed.

---

## Architecture

```
                    ┌─────────────────────────┐
                    │        create_app       │
                    │  resolve → register →   │
                    │  startup → serve        │
                    └───────────┬─────────────┘
                                │
         ┌──────────────┬───────┴───────┬──────────────┐
         │              │               │              │
  ┌──────▼──────┐ ┌─────▼─────┐  ┌──────▼──────┐ ┌─────▼─────┐
  │observability│ │  database │  │   qdrant    │ │    web    │
  │  provides:  │ │ provides: │  │  provides:  │ │ provides: │
  │   logger    │ │ db.engine │  │qdrant.client│ │  render   │
  └─────────────┘ └─────┬─────┘  └──────┬──────┘ └───────────┘
                        │               │
                        └───────┬───────┘
                                │
                         ┌──────▼──────┐
                         │     rag     │  picks one at build time,
                         │ rag.store   │  fails loudly if it is absent
                         └─────────────┘
```

Plugins never import each other. They publish objects under string keys and
consume them the same way. That indirection is what makes any plugin
swappable — including the built-ins.

Full detail and trade-offs in [ARCHITECTURE.md](ARCHITECTURE.md).

## Repository layout

```
src/jfastframework/
├── app.py              create_app: the only entry point a service needs
├── settings.py         typed config, jfast.toml + env
├── context.py          AppContext: provide/require between plugins
├── errors.py           RFC 7807 problem+json
├── health.py           /health, /ready, /info
├── plugins/
│   ├── base.py         the Plugin contract, including infra()
│   ├── registry.py     discovery, selection, dependency ordering
│   └── builtin/        the nine plugins above
├── vectors/            VectorStore protocol, pgvector and Qdrant stores
├── db/                 declarative Base, naming convention, BaseRepository
├── deploy/             compose and Dockerfile generation from plugin infra
├── cli/                the `jfast` command
├── templates/          Jinja2 files — never string literals in Python
│   ├── module_layered/ ├── module_screaming/ ├── ui_htmx/
│   └── service_base/   └── service_web/
└── testing/            fixtures so services can test in five lines

.jfast/skills/          task instructions for AI agents
docs/                   modules, datastores, plugins, skills, deployment
legacy/                 the v0 prototype, kept for reference
```

## Documentation

| Document | Contents |
| --- | --- |
| [PLAN.md](PLAN.md) | Roadmap by phase, with what is done and what is not |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Design decisions and their trade-offs |
| [AGENTS.md](AGENTS.md) | Rules for AI agents and contributors |
| [docs/modules.md](docs/modules.md) | Services, module layouts, HTMX |
| [docs/datastores.md](docs/datastores.md) | Choosing PostgreSQL, Redis, Mongo, Qdrant |
| [docs/plugins.md](docs/plugins.md) | Writing a plugin |
| [docs/skills.md](docs/skills.md) | Writing a skill |
| [docs/deploy.md](docs/deploy.md) | Deployment targets |

## Verify

```bash
pytest              # 59 framework tests
ruff check src tests
mypy src
bash scripts/smoke.sh   # scaffolds both layouts, boots a web service, generates compose
```

## License

MIT.
