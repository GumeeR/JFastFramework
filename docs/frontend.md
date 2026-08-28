# Frontends

Two ways to put a UI in front of a JFast backend. Pick by whether you want a
build step.

| | `--kind web` | `--kind spa` |
| --- | --- | --- |
| Renders | Server-side Jinja2 + HTMX | Vue 3 or React, client-side |
| Build step | none | Vite |
| Lives | inside the backend service | its own service |
| Good for | admin panels, internal tools, CRUD | rich client state, offline, mobile-ish UX |

`--kind web` is covered in [modules.md](modules.md). This page is about
`--kind spa`.

---

## Create one

```bash
jfast new service admin --kind spa --frontend vue
cd admin
npm install
npm run dev
```

React instead:

```bash
jfast new service portal --kind spa --frontend react
```

Angular is **not** generated. See "Angular" at the bottom.

## The API URL is already right

`.env` is written from `jfast.workspace.toml`:

```
VITE_API_URL=http://localhost:8030
```

That points at the gateway when the workspace has one and at the single backend
when it does not. The generated home page calls `/health` through it on first
load and shows the result, so a wrong value surfaces immediately instead of on
your first real feature.

After adding a backend (or the day a gateway appears):

```bash
jfast workspace env
```

---

## Adding a module

This is the part modelled on the generator you already had:

```bash
jfast new view Facturas
```

```
src/ModuloFacturas/
├── Components/
│   ├── Modals/
│   └── Tables/
├── Pages/FacturasView.vue
├── Routes/router.js
└── Services/facturas.service.js
```

then it registers the module in two places:

```js
// src/router/index.js
import { ModuloFacturas } from '@/ModuloFacturas/Routes/router.js'
...
  ...ModuloFacturas,
  /*nuevaRuta*/
```

```js
// src/menuAside.js
import { mdiHomeOutline, mdiViewDashboardOutline } from '@mdi/js'
...
  {
    to: '/facturas',
    icon: mdiViewDashboardOutline,
    label: 'Facturas',
  },
  /*nuevoModulo*/
```

The framework is detected from the project, so you do not repeat
`--frontend react` inside a React project.

### The markers

Keep `/*nuevaRuta*/` and `/*nuevoModulo*/`. Three properties are guaranteed,
and each one is a failure mode that is otherwise silent:

| Property | Without it |
| --- | --- |
| **Idempotent** — a guard string is checked first | Two routes and two sidebar entries per re-run |
| **Loud** — a missing file or marker raises with the path | A blank page and no explanation |
| **Marker-preserving** — the marker is written back after the block | The second module has nowhere to go |

A reformatted marker (`/* nuevaRuta */`) still matches: matching strictly would
turn a `prettier` run into a silent no-op.

### Naming

`Facturas` → `ModuloFacturas`, `/facturas`, `facturas.service.js`.
`BillingAccount` → `ModuloBillingAccount`, `/billing-account`, and the sidebar
label reads `Billing Account`.

---

## Conventions the templates enforce

**One axios instance.** `src/services/api.js` holds the base URL, the timeout,
and an interceptor that turns the backend's RFC 7807 `detail` into
`error.message`. Without it every component shows "Request failed with status
code 409" instead of "Invoice INV-1 already exists".

**HTTP in `Services/`, never in components.** The generated page calls
`listFacturas()`; it owns loading and error state, the service owns the request.

**Tailwind v4 with tokens.** One `@import "tailwindcss"` and an `@theme` block
in `src/style.css`. Add a token there before using a value — a raw hex in a
component is how a design system stops existing. See
[.jfast/skills/design-system/SKILL.md](../.jfast/skills/design-system/SKILL.md).

**Icons without a runtime.** `@mdi/js` ships path strings only; `BaseIcon`
renders one into an SVG. No icon font, tree-shaken to what you import.

Vue specifics: `<script setup>` and the Composition API, never Options API;
components `PascalCase.vue`; route-level pages end in `View.vue`.

React specifics: function components and hooks; components `PascalCase.jsx`;
route-level pages end in `View.jsx`.

---

## Verified how

Be precise about this, because it matters for how much you should trust it.

**Tested in CI:** every file renders; the module structure lands where it
should; router and sidebar are patched correctly and idempotently; Vue's `{{ }}`
and JSX's braces survive scaffolding; the framework is detected from the
project.

**Not tested:** `npm install` and `npm run dev`. There is no Node in the test
environment, so the dependency versions in `package.json` and the Vite/Tailwind
wiring are written from knowledge, not from a green build. Run `npm install`
once and report anything that breaks — that is the gap.

---

## Angular

Not generated, deliberately. A hand-rolled `angular.json` and builder config
that has never been run by `ng serve` is worse than no scaffold: it looks
finished and fails in a way that is hard to attribute.

If you want Angular today: `ng new` the project yourself, then keep the same
`Modulo<Name>` structure by hand. Generator support is PLAN.md phase 3, and it
should land only alongside a CI job that actually builds the output.
