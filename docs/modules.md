# Services, modules and layouts

Two levels of generation:

```bash
jfast new service billing            # a whole service
jfast new module invoice             # a domain module inside it
```

Both are compositions, not fixed templates. You choose the shape.

---

## Services

```bash
jfast new service billing                          # JSON API
jfast new service storefront --kind web --port 8020  # server-rendered frontend
```

| Kind | Renders | Enabled plugins | Extra needed |
| --- | --- | --- | --- |
| `api` | JSON | observability, metrics, database | `[server,db,metrics]` |
| `web` | HTML | + web | `[server,db,metrics,web]` |

A frontend is a service like any other. It logs the same way, reports health
the same way, deploys the same way, and lives in the same port block. The only
difference is what comes out of the handlers.

Both kinds are generated with `main.py`, `jfast.toml`, `.env.example`,
`conftest.py`, `requirements.txt`, `.gitignore` and a README. The `web` kind
adds `templates/base.html`, `templates/index.html`, `static/app.css` and a root
`web.py` router.

---

## Module layouts

```bash
jfast new module invoice                      # layered  (default)
jfast new module invoice --layout screaming   # use case per file
```

### `layered`

The familiar shape. Good when the module is mostly CRUD and the interesting
part is the data, not the rules.

```
modules/invoice/
├── router.py       HTTP in, response out
├── service.py      business rules
├── repository.py   queries
├── models.py       SQLAlchemy
├── schemas.py      Pydantic
├── README.md
└── tests/
```

### `screaming`

The directory listing is the feature list. Good when the module has real rules
worth protecting from the framework, and when you want new capabilities to
arrive as new files rather than as new methods on a class nobody can navigate.

```
modules/invoice/
├── invoice.py           the domain: entity + rules, framework-free
├── use_cases/
│   ├── create_invoice.py
│   ├── list_invoices.py
│   ├── get_invoice.py
│   ├── update_invoice.py
│   └── delete_invoice.py
├── storage.py           SQLAlchemy model + repository, with to_domain()
├── http.py              router + wire schemas
├── README.md
└── tests/
    ├── test_invoice_domain.py      no database, no fakes, no event loop
    └── test_invoice_use_cases.py   fake repository, nothing else
```

Imports only ever point inward: `http` knows `use_cases`, `use_cases` knows the
domain, the domain knows nothing. `invoice.py` importing SQLAlchemy is the
signal that a rule ended up in the wrong file.

**Which to pick.** Layered for CRUD. Screaming when the rules matter, when more
than one entry point (HTTP, worker, CLI) drives the same behaviour, or when the
module is going to be worked on by several people over a long time.

Both layouts export the same `build_service(session, tenant_id)` factory. That
shared surface is what lets the HTMX overlay — and anything else — consume a
module without knowing how it is organised inside.

---

## UI: JSON, HTML, or both

```bash
jfast new module invoice --ui api    # JSON only (default)
jfast new module invoice --ui htmx   # JSON plus server-rendered pages
```

`--ui htmx` is an *overlay*, composed on top of either layout rather than
duplicated per layout. It adds:

```
modules/invoice/web.py           HTML router
templates/invoice/index.html     the page
templates/invoice/_rows.html     the table body fragment
templates/invoice/_row.html      one row
```

The JSON router stays. A module can serve both surfaces from the same rules,
which is the point: the HTML views are not a second implementation.

### Partial rendering

HTMX sends `HX-Request: true` and expects a fragment. `render()` handles both
from one handler:

```python
return render(request, "invoice/index.html", {"page": page},
              partial="invoice/_rows.html")
```

Browser navigation gets the whole page. `hx-get` gets just the rows. One
handler, one context, no duplicated markup — the page `{% include %}`s the same
fragment it returns.

### Errors

With the `web` plugin enabled, a `JFastError` raised during an HTMX request
comes back as an HTML fragment instead of `problem+json`. HTMX swaps the
response body into the DOM, so JSON would render to the user as raw text. Plain
requests still get `problem+json`, which keeps a mixed API/web service honest on
both surfaces.

---

## Table names

Table names are pluralised: `order` → `orders`, `category` → `categories`.
Not cosmetics — singular nouns collide with SQL reserved words far more often
than plurals (`order`, `user`, `group`). Override when it guesses wrong:

```bash
jfast new module order --table sales_orders
```

---

## The four combinations

| Command | Result |
| --- | --- |
| `jfast new module invoice` | Layered, JSON |
| `jfast new module invoice --layout screaming` | Use-case files, JSON |
| `jfast new module invoice --ui htmx` | Layered, JSON + pages |
| `jfast new module invoice --layout screaming --ui htmx` | Use-case files, JSON + pages |

Three template trees produce all four. Composition rather than four copies that
drift apart.

---

## After generating

```bash
# mount it
#   from modules.invoice import router as invoice_router
#   from modules.invoice.web import router as invoice_web_router   # --ui htmx

pytest modules/invoice/tests
alembic revision --autogenerate -m "add invoices"
alembic upgrade head
```

Read the generated migration before applying it. Autogenerate misses
server-side defaults, enum changes and index renames.

Generated files carry a `.jfast-template` stamp recording which template and
which framework version produced them. That stamp is what a future
`jfast upgrade` uses to show a diff instead of a rewrite.
