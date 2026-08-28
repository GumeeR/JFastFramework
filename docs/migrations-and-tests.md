# Migrations and tests

Both are wired into every generated service. Neither is something you set up.

---

## Migrations (Alembic)

`jfast new service` writes `alembic.ini`, `migrations/env.py`,
`migrations/script.py.mako` and `migrations/versions/` whenever the `database`
plugin is enabled.

```bash
alembic revision --autogenerate -m "add invoices"
alembic upgrade head
alembic downgrade -1
alembic history
```

Two things it does that a stock `alembic init` does not:

**The DSN comes from the application's settings.** `env.py` reads
`JFAST_DB_DSN` through the same `DatabaseSettings` the service uses, and
`alembic.ini` deliberately has no `sqlalchemy.url`. A migration that *can* run
against a different database than the service eventually will, at the worst
possible moment.

**Models are imported automatically.** Autogenerate only sees tables whose
classes have been imported. A forgotten import produces an empty migration, and
the missing table is discovered in production. `env.py` walks `modules/` and
imports each module's `models.py` (layered layout) or `storage.py` (screaming
layout), so both work without you maintaining an import list.

It also enables `compare_type` and `compare_server_default` — without them
autogenerate silently misses column type changes and default changes, the two
edits people most often assume it caught.

### Constraint names are pinned

`jfastframework.db.Base` sets a `naming_convention`. Without it PostgreSQL
invents constraint names and autogenerate produces different diffs on different
machines. With it, a primary key is always `pk_<table>`, a foreign key always
`fk_<table>_<column>_<referred>`.

Adopting it on a database that already has auto-named constraints needs a
one-time migration. Do that before the fleet grows.

### Read the migration before applying it

Autogenerate is a draft, not a plan:

- A **rename** is rendered as a drop plus an add. On a table with rows, that is
  silent data loss.
- **Data migrations** are not written at all.
- Index renames and enum changes are frequently missed.

### Offline SQL

```bash
alembic upgrade head --sql > migration.sql
```

Runs `env.py` without connecting, which is also how CI verifies the wiring
without a database.

---

## Tests (pytest)

`jfast new service` writes `pytest.ini` and `conftest.py`. Generated modules
bring their own tests that pass immediately:

```bash
pytest                          # modules/ and tests/
pytest modules/invoice/tests    # one module
```

### Fixtures

`conftest.py` provides `app` and `client`, built with an **explicit** plugin
list:

```python
@pytest.fixture
def app():
    return build_test_app(plugins=["observability"], app_name="billing")
```

Explicit beats implicit here: a test that names its plugins cannot break
because someone changed a default in `jfast.toml`.

From `jfastframework.testing`:

| Helper | Does |
| --- | --- |
| `build_test_app(...)` | App with a named plugin list, no config file, no env |
| `client_for(app)` | Async HTTP client with the lifespan actually executed |
| `NullPlugin` | Records `register` / `startup` / `shutdown`, for ordering tests |
| `make_config(...)` | A `JFastConfig` without touching disk |

`client_for` matters more than it looks: plugins that open resources in
`startup` need the lifespan to run, and a bare `TestClient` skips it in async
contexts.

### What the generated tests actually test

**Layered layout** — service-level tests against a fake repository. No
database, no containers.

**Screaming layout** — two files, deliberately separated:

- `test_<module>_domain.py` — the entity and its rules. No database, no fakes,
  no event loop. If a test here ever needs a fixture, a rule has leaked out of
  the domain.
- `test_<module>_use_cases.py` — a fake repository and an event loop, nothing
  else.

Both mark themselves with `pytest.mark.asyncio` explicitly rather than relying
on `asyncio_mode = auto`, so they pass in a project that has not configured
pytest-asyncio.

### Integration tests

Tests needing a real PostgreSQL or Redis are not generated. Mark them
`@pytest.mark.integration` and keep them out of the fast suite; a framework-level
integration harness is PLAN.md phase 6.

---

## What CI should run

```bash
pytest
ruff check src tests
mypy src
jfast doctor
```

Plus, for anything that touches templates:

```bash
bash scripts/smoke.sh              # renders both layouts, runs their tests, checks alembic
bash scripts/smoke_workspace.sh    # workspace, gateway, frontends, patching
```

Templates are the part that breaks silently — they render fine and produce code
that does not import. `pytest` alone does not catch that.
