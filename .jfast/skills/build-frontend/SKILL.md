---
name: build-frontend
description: Build a UI for a JFast backend — server-rendered with HTMX, or a
  Vue/React SPA — and generate its modules.
when_to_use: The user wants a UI, an admin panel, a dashboard, a CRUD screen,
  or "a frontend for this API".
when_not_to_use: They want Angular (not generated — see the bottom), or the
  surface is machine-to-machine and needs no UI at all.
---

## Step 1: pick the kind

| | `--kind web` | `--kind spa` |
| --- | --- | --- |
| Renders | Jinja2 + HTMX, server-side | Vue 3 or React, client-side |
| Build step | none | Vite |
| Lives | inside the backend service | its own service, its own port block |
| Pick when | admin panels, internal tools, CRUD | rich client state, offline, app-like UX |

If the user has not said, **default to `web`**. It has no build step, no second
deploy target, and no API/SPA split to maintain. Reach for `spa` when the UI
genuinely needs client-side state.

---

## Path A — server-rendered (`--kind web`)

```bash
jfast new service storefront --kind web
cd storefront
pip install -r requirements.txt
jfast new module product --ui htmx
```

Mount both routers in `main.py`:

```python
from modules.product import router as product_router
from modules.product.web import router as product_web_router

ROUTERS = [web_router, product_router, product_web_router]
```

### Partial rendering — the one idea to get right

HTMX sends `HX-Request: true` and expects a *fragment*:

```python
return render(request, "product/index.html", {"page": page},
              partial="product/_rows.html")
```

Browser navigation renders the page; `hx-get` renders only the rows. The page
`{% include %}`s the same fragment it returns, so the markup exists once.

Rules that keep this from rotting:

- Every fragment is its own template, named with a leading underscore.
- The full page includes its fragments. Never copy markup between them.
- A handler reachable both ways always passes `partial=`.
- Return the smallest thing that changed. Creating a row returns the row.

### Errors

A `JFastError` raised during an HTMX request returns an HTML fragment; a plain
request still gets `problem+json`. Not cosmetic: HTMX swaps the body into the
DOM, so a JSON error renders to the user as raw text. Give the page somewhere
to put it — the generated form has `<div class="form-error">`.

---

## Path B — SPA (`--kind spa`)

```bash
jfast new service admin --kind spa --frontend vue    # or react
cd admin
npm install
npm run dev
```

`VITE_API_URL` is already written from `jfast.workspace.toml` — the gateway if
there is one, the single backend if not. The home page calls `/health` through
it on load, so a wrong value shows up immediately.

After adding a backend, or the day a gateway appears:

```bash
jfast workspace env
```

### Add a module

```bash
jfast new view Facturas
```

Creates `src/ModuloFacturas/{Components/{Modals,Tables},Pages,Routes,Services}`
and registers it in `src/router/index.js` at `/*nuevaRuta*/` and in
`src/menuAside.js` at `/*nuevoModulo*/`.

The framework is detected from the project — do not pass `--frontend` again
inside an existing project.

**Never remove those markers.** With them, re-running is idempotent and a
missing marker raises with the file path. Without them, the generator has
nowhere to write and the failure is a blank page.

### Conventions the templates enforce

- HTTP lives in `Services/`, never in a component. Everything goes through the
  single axios instance in `src/services/api.js`, which already turns the
  backend's RFC 7807 `detail` into `error.message`.
- Tailwind v4 utilities only. Tokens live in `@theme` in `src/style.css` — add
  a token before using a value, never a raw hex in a component.
- Vue: `<script setup>`, Composition API, pages end in `View.vue`.
- React: function components and hooks, pages end in `View.jsx`.

---

## Design

Before writing components, read `.jfast/skills/design-system/SKILL.md` and the
project's `DESIGN.md`. The generated stylesheet is a token baseline so the
first screen is not unstyled — it is meant to be replaced, not extended
ad hoc.

## Verification

Server-rendered:

```bash
curl -s localhost:8000/products | head -3
curl -s -H 'HX-Request: true' localhost:8000/products | head -3
```

The two must differ — full document versus fragment. Identical output means the
handler is not passing `partial=`.

SPA:

```bash
npm run dev
```

Then in the browser: the home page must show the backend as reachable. If it
does not, `VITE_API_URL` is wrong or the backend is down — check that before
touching any component.

Either way, create and delete a record and confirm the page does not reload.

## Common mistakes

- Duplicating markup between a page and its fragment instead of including it.
- Returning the whole table after creating one row.
- Enabling a module's HTML router without the `web` plugin — `ctx.require("render")`
  will say so.
- Calling axios directly from a component instead of through `Services/`.
- Hardcoded colours instead of tokens.
- No empty state. A table that renders nothing when there is nothing reads as
  a bug.

## Angular

Not generated. A hand-rolled `angular.json` that has never been run by
`ng serve` looks finished and fails in a way that is hard to attribute. If the
user needs Angular: `ng new` the project, then keep the same `Modulo<Name>`
structure by hand. Generator support is PLAN.md phase 3, gated on a CI job that
actually builds the output.

**Also be honest about Vue and React:** the scaffolds are verified in CI to
render, patch and structure correctly, but `npm install` and `vite build` have
never run against them. Say so when handing one over.
