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

### Timestamps carry a zone (breaking, needs a one-time migration)

`TimestampMixin` used to map `created_at` / `updated_at` to
`TIMESTAMP WITHOUT TIME ZONE`. Values came back as `2026-08-29T20:55:15` —
no `Z`, no offset — and every JavaScript client read that as *local* time, so
a row written now rendered hours away for anyone off UTC. The mixin now uses
`jfastframework.db.UTCDateTime`, which is `TIMESTAMPTZ` on PostgreSQL and
attaches UTC on the way out everywhere else.

Every table built on the mixin needs converting once. `autogenerate` sees the
type change (`compare_type` is on) and writes a bare
`ALTER COLUMN ... TYPE timestamptz` with no `USING`. That does not fail — it
converts through the implicit cast, which reads every stored value in the
*server's* `TimeZone`. On a server not set to UTC that shifts the whole table
and nothing complains. Write it by hand instead:

```sql
ALTER TABLE invoices
    ALTER COLUMN created_at TYPE timestamptz USING created_at AT TIME ZONE 'UTC',
    ALTER COLUMN updated_at TYPE timestamptz USING updated_at AT TIME ZONE 'UTC';
```

`AT TIME ZONE 'UTC'` is the load-bearing part: it states that the stored values
were UTC all along. They were — `now()` written into a `timestamp` column
stored the UTC instant with the zone stripped.

The `USING` form rewrites the table and holds an `ACCESS EXCLUSIVE` lock while
it does. Schedule it like any other rewrite on a large table.

Writes are stricter afterwards: a naive `datetime` raises rather than being
stored under an assumed zone. Use `datetime.now(UTC)`.

### Read the migration before applying it

Autogenerate is a draft, not a plan:

- A **rename** is rendered as a drop plus an add. On a table with rows, that is
  silent data loss. The generated revision says so, but only when it actually
  contains a drop and an add on the same table — a warning in every revision is
  one nobody reads.
- **Data migrations** are not written at all.
- Index renames and enum membership changes are frequently missed. See
  [Enums](datastores.md#enums-which-half-of-the-guarantee-you-are-buying) for
  what the column does and does not enforce either way.
- A **new `NOT NULL` column** is repaired for you when the model gives a scalar
  `default=`: the revision adds the column with a matching `server_default`,
  backfills, and drops the default again in the same migration. Alembic only
  looks at `server_default`, so without this it emitted DDL PostgreSQL rejects
  outright on any table that has rows. A `default=` it cannot turn into SQL — a
  callable like `uuid4`, or no default at all — is announced in the revision
  instead, because there is nothing to backfill with.

### Read it with `jfast migration check`

```bash
jfast migration check              # every unapplied revision
jfast migration check --all        # applied ones too
jfast migration check --json       # for an agent, or CI
jfast migration plan               # the next risky revision, and the safe rewrite
```

`check` parses `migrations/versions/*.py` with `ast` and **never imports them**.
A revision imports the project's models, and the environment the CLI runs in is
usually not the environment those imports resolve in — a checker that only works
when the project already imports is unavailable exactly when it is needed.

| Finding | Severity | What it means |
| --- | --- | --- |
| `migration-add-not-null` | critical | `add_column` with `nullable=False` and no `server_default`. PostgreSQL rejects it outright the moment the table has one row |
| `migration-rename` | critical | An `add_column` and a `drop_column` on the same table in one revision. Autogenerate renders a rename exactly like this, and the data goes with the drop |
| `migration-timestamptz-no-using` | critical | `ALTER COLUMN ... TYPE timestamptz` with no `USING`. Does not fail; silently shifts the column. See above |
| `migration-drop-table` | critical | Every row is lost and `downgrade` recreates the table empty at best |
| `migration-drop-column` | high | The column and its contents are gone; `downgrade` brings back an empty column |
| `migration-type-change` | high | Rewrites the table under `ACCESS EXCLUSIVE`: no reads, no writes, until it finishes |
| `migration-set-not-null` | high | `alter_column(nullable=False)` scans the whole table to validate, holding the lock |
| `migration-drop-constraint` | medium | The guarantee stops holding immediately; re-adding it needs a validating scan |
| `migration-index-lock` | medium | `create_index` without `postgresql_concurrently=True` blocks every write for the duration |
| `migration-no-downgrade` | low | Not a defect. But `alembic downgrade -1` will report success and change nothing |

Severities, the `Finding` shape and `--fail-on` are the same ones `jfast analyze`
uses. `--fail-on` defaults to `high`; a risk at or above it exits **4**
(`Code.MIGRATION`).

Deliberately absent: anything not decidable from the source text. Arbitrary
`op.execute` SQL is only read for the `timestamptz` conversion above, because a
general answer needs a SQL parser and a wrong one is worse than none. A
`create_index` or an `alter_column` on a table the same revision creates is not
reported at all — that table is empty by construction, and reporting it is the
false positive that gets the whole command muted. Widening a `VARCHAR` is not
reported either: PostgreSQL takes a longer `varchar` as a catalogue edit, not a
rewrite.

#### How this relates to the `env.py` hook

They are two halves of the same problem at different moments.
`migrations/env.py` installs a `process_revision_directives` hook that repairs a
scalar-default `NOT NULL` column **while the revision is being generated** — the
one case where the fix is derivable from the model. `migration check` reads
revisions that **already exist**: hand-written ones, ones merged from a branch,
and ones generated before that hook existed. A revision autogenerated by a
current service should therefore never trip `migration-add-not-null`. If one
does, it was written by hand or generated by an older version, and the finding
is correct.

#### Row counts need a database

The `Reason` line in `plan` states a real row count when a DSN resolves —
`--dsn`, then `JFAST_DB_DSN`, then `.env` in the project root:

```
Migration:  0004_add_status
Risk:       CRITICAL
Reason:     status is NOT NULL and widgets has rows

Recommended:
  1. add the column nullable
  2. backfill it
  3. add the NOT NULL constraint
```

When none resolves, it says `has an unknown row count, treat as populated`. It
never reports a table as empty on no evidence: a checker that assumes the safe
case is a checker that goes quiet in production. `--no-db` skips the connection
entirely, which is what CI should use.

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
