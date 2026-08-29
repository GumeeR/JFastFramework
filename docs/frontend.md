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

## Base components

Both frontends ship the same set, so the two are the same product rather than
two projects that happen to share a backend.

| Component | What it handles for you |
| --- | --- |
| `BaseButton` | `primary` / `outline` / `ghost` / `danger`, two sizes, and a loading state that also disables it |
| `BaseInput` | Label, error text, hint, disabled — it is meant to be impossible to ship a bare unlabelled input |
| `BaseModal` | Backdrop blur, Escape and backdrop click, focus trapped while open and returned to the trigger on close |
| `BaseBadge` | Status colours, with the word always shown |
| `SkeletonLoader` | The shape of the content that is coming |
| `EmptyState` | A heading, a sentence, and the action that creates the first one |
| `ToastHost` | Renders the toast queue; mounted once at the app root |

**A destructive button is outlined, never solid.** Solid red is already taken
by "proceed", and a screen where the accent means two opposite things has no
accent.

### The two states people forget

**Loading is not the word "Loading".** `SkeletonLoader` takes props for lines
and shape so it can be made the shape of what is arriving — which is what stops
the layout jumping when it does.

**Empty is not an empty page.** `EmptyState` says what would be there and
offers the action that creates it. A blank table reads as a bug.

### Toasts

```js
// Vue
import { useToast } from '@/composables/useToast.js'
const toast = useToast()
toast.success('Invoice created')

// React
import useToast from '@/hooks/useToast.js'
const toasts = useToast((state) => state.toasts)

// Anywhere that cannot call a hook — an interceptor, a loader, a guard:
import { toast } from '@/stores/notification.store.js'
toast.error('Could not reach the server')
```

One store, two doors. A toast raised deep inside a service and one raised in a
component land in the same list, in order, under the same timers. Two stores
would mean a toast raised from a service goes into a list the host does not
render — nothing on screen, and no error either.

Timers live outside the store and are cleared on manual dismiss. A dismissed
toast still has a timeout aimed at it; left alive, every manual dismiss leaks
one that later writes state for a toast that is gone.

---

## State

| | Vue | React |
| --- | --- | --- |
| Library | Pinia | Zustand |
| Auth | `src/stores/auth.store.js` | same path |
| Notifications | `src/stores/notification.store.js` | same path |

Both ship wired up: Pinia is installed on the app, Zustand is in
`package.json`, and the auth store persists its token the same way
`services/api.js` already reads it — one source of truth, named in a comment in
the file.

**When not to use a store.** State that no other component reads belongs in the
component. A store is for something two unrelated places need: the layout reads
the user, axios needs the token. That is two, so it is a store.

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
project. And, since the base components landed, **`npm install` followed by
`npm run build` on both generated frontends** — `scripts/smoke_components.sh`,
which also asserts that `ToastHost` is mounted somewhere and that there is
exactly one toast store. Rendering a template proves the braces were right; it
does not prove a component imports something that exists.

**Not tested:** `npm run dev` as an interactive session, and anything about how
it looks. The build passing means it compiles, not that a modal traps focus
correctly in a real browser with a real screen reader.

---

## Angular

Not generated, deliberately. A hand-rolled `angular.json` and builder config
that has never been run by `ng serve` is worse than no scaffold: it looks
finished and fails in a way that is hard to attribute.

If you want Angular today: `ng new` the project yourself, then keep the same
`Modulo<Name>` structure by hand. Generator support is PLAN.md phase 3, and it
should land only alongside a CI job that actually builds the output.
