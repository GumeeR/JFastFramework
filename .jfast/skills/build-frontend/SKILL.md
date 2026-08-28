---
name: build-frontend
description: Build a server-rendered frontend (Jinja2 + HTMX) as a JFast
  service, with pages backed by domain modules.
when_to_use: The user wants a UI, an admin panel, a dashboard, a CRUD screen,
  or "a frontend for this API" -- and has not asked specifically for a
  single-page app in React or Vue.
when_not_to_use: They explicitly want a SPA framework (not implemented -- see
  PLAN.md phase 3), or the surface is machine-to-machine and needs no HTML.
---

## The shape

A frontend is a JFast service like any other: same logs, same `/health`, same
port block, same deploy path. It returns HTML instead of JSON.

No build step, no bundler, no separate repository. HTMX gives you the
interactivity most internal tools need without any of that.

## Steps

1. **Create the service.**

   ```bash
   jfast new service storefront --kind web --port 8020
   cd storefront
   pip install "jfastframework[server,db,metrics,web]"
   cp .env.example .env
   ```

   You get `templates/base.html`, `templates/index.html`, `static/app.css`, a
   root `web.py`, and the `web` plugin already enabled in `jfast.toml`.

2. **Add a module with pages.**

   ```bash
   jfast new module product --ui htmx
   ```

   This writes the JSON router *and* `modules/product/web.py` plus
   `templates/product/{index,_rows,_row}.html`. Both surfaces run on the same
   rules — the HTML views are not a second implementation.

3. **Mount both routers** in `main.py`:

   ```python
   from modules.product import router as product_router
   from modules.product.web import router as product_web_router

   ROUTERS = [web_router, product_router, product_web_router]
   ```

4. **Migrate and run.**

   ```bash
   alembic revision --autogenerate -m "add products"
   alembic upgrade head
   uvicorn main:app --reload --port 8020
   ```

## Partial rendering — the one idea to get right

HTMX sends `HX-Request: true` and expects a *fragment*, not a page.

```python
return render(request, "product/index.html", {"page": page},
              partial="product/_rows.html")
```

Browser navigation renders the page; `hx-get` renders only the rows. The page
`{% include %}`s the same fragment it returns, so the markup exists once.

Rules that keep this from rotting:

- Every fragment is its own template, named with a leading underscore.
- The full page includes its fragments. Never copy markup between them.
- A handler that can be reached both ways always passes `partial=`.
- Return the smallest thing that changed. Creating a row returns the row, not
  the table.

## Errors

With the `web` plugin enabled, a `JFastError` raised during an HTMX request
returns an HTML fragment; a plain request still gets `problem+json`. This is
not cosmetic: HTMX swaps the response body into the DOM, so a JSON error would
render to the user as raw text.

Give the page somewhere to put it — the generated form includes
`<div class="form-error">`, and `base.html` wires `htmx:responseError` to it.

## Design

Before writing components, read `.jfast/skills/design-system/SKILL.md` and the
project's `DESIGN.md`. The generated `static/app.css` is a token baseline, not
a design system: it exists so the first screen is not unstyled, and it is meant
to be replaced.

Do not invent colours or spacing per template. Add the value to the token file
first, then use it.

## Verification

```bash
jfast doctor
curl -s localhost:8020/ready | jq '.checks.web'
curl -s localhost:8020/products | head -20
curl -s -H 'HX-Request: true' localhost:8020/products | head -5
```

The last two must differ: the first is a full document, the second is a
fragment with no `<html>`. If they are identical, the handler is not passing
`partial=`.

Then, in a browser: create a row, delete a row, and confirm neither reloads the
page.

## Common mistakes

- Duplicating markup between the page and its fragment instead of including it.
- Returning the whole table after creating one row.
- Enabling the module's HTML router without enabling the `web` plugin — the
  render provider will not exist, and `ctx.require("render")` says so.
- Hardcoded colours in templates instead of tokens.
- No empty state. A table that renders nothing when there is nothing reads as
  a bug.
