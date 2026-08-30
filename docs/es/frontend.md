# Frontends

Dos maneras de poner una UI delante de un backend JFast. Elige según si
quieres un paso de build o no.

| | `--kind web` | `--kind spa` |
| --- | --- | --- |
| Renderiza | Jinja2 + HTMX del lado del servidor | Vue 3 o React, del lado del cliente |
| Paso de build | ninguno | Vite |
| Vive | dentro del servicio backend | en su propio servicio |
| Bueno para | paneles de administración, herramientas internas, CRUD | estado de cliente rico, offline, UX tipo móvil |

`--kind web` está cubierto en [modules.md](modules.md). Esta página trata de
`--kind spa`.

---

## Crear uno

```bash
jfast new service admin --kind spa --frontend vue
cd admin
npm install
npm run dev
```

React en su lugar:

```bash
jfast new service portal --kind spa --frontend react
```

Angular **no** se genera. Ver "Angular" al final.

## La URL de la API ya está bien

`.env` se escribe a partir de `jfast.workspace.toml`:

```
VITE_API_URL=http://localhost:8030
```

Eso apunta al gateway cuando el workspace tiene uno, y al único backend cuando
no. La home page generada llama a `/health` a través de él en la primera carga
y muestra el resultado, así que un valor equivocado aparece de inmediato en vez
de en tu primera feature real.

Después de agregar un backend (o el día que aparezca un gateway):

```bash
jfast workspace env
```

---

## Agregar un módulo

Esta es la parte modelada sobre el generador que ya tenías:

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

y después registra el módulo en dos lugares:

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

El framework se detecta desde el proyecto, así que no repites
`--frontend react` dentro de un proyecto React.

### Los marcadores

Conserva `/*nuevaRuta*/` y `/*nuevoModulo*/`. Tres propiedades están
garantizadas, y cada una es un modo de falla que de otro modo es silencioso:

| Propiedad | Sin ella |
| --- | --- |
| **Idempotente** — primero se revisa una cadena de guarda | Dos rutas y dos entradas de sidebar por cada re-ejecución |
| **Ruidoso** — un archivo o marcador faltante lanza error con la ruta | Una página en blanco y ninguna explicación |
| **Preserva el marcador** — el marcador se vuelve a escribir después del bloque | El segundo módulo no tiene a dónde ir |

Un marcador reformateado (`/* nuevaRuta */`) igual coincide: hacer el match
estricto convertiría una corrida de `prettier` en un no-op silencioso.

### Nombres

`Facturas` → `ModuloFacturas`, `/facturas`, `facturas.service.js`.
`BillingAccount` → `ModuloBillingAccount`, `/billing-account`, y la etiqueta
del sidebar dice `Billing Account`.

---

## Componentes base

Ambos frontends traen el mismo set, así que los dos son el mismo producto en
vez de dos proyectos que casualmente comparten un backend.

| Componente | Qué resuelve por ti |
| --- | --- |
| `BaseButton` | `primary` / `outline` / `ghost` / `danger`, dos tamaños, y un estado de carga que además lo deshabilita |
| `BaseInput` | Label, texto de error, hint, disabled — está pensado para que sea imposible mandar a producción un input pelado sin label |
| `BaseModal` | Blur del fondo, Escape y clic en el fondo, foco atrapado mientras está abierto y devuelto al disparador al cerrar |
| `BaseBadge` | Colores de estado, con la palabra siempre visible |
| `SkeletonLoader` | La forma del contenido que viene en camino |
| `EmptyState` | Un encabezado, una frase, y la acción que crea el primero |
| `ToastHost` | Renderiza la cola de toasts; se monta una vez en la raíz de la app |

**Un botón destructivo va outlined, nunca sólido.** El rojo sólido ya está
tomado por "continuar", y una pantalla donde el acento significa dos cosas
opuestas no tiene acento.

### Los dos estados que la gente olvida

**Cargando no es la palabra "Cargando".** `SkeletonLoader` recibe props para
líneas y forma, así que se le puede dar la forma de lo que está llegando — que
es lo que evita que el layout salte cuando llega.

**Vacío no es una página vacía.** `EmptyState` dice qué estaría ahí y ofrece la
acción que lo crea. Una tabla en blanco se lee como un bug.

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

Un store, dos puertas. Un toast lanzado desde el fondo de un service y uno
lanzado en un componente caen en la misma lista, en orden, bajo los mismos
timers. Dos stores significarían que un toast lanzado desde un service va a una
lista que el host no renderiza — nada en pantalla, y tampoco un error.

Los timers viven fuera del store y se limpian al descartar manualmente. Un
toast descartado igual tiene un timeout apuntándole; si se lo deja vivo, cada
descarte manual filtra uno que después escribe estado para un toast que ya no
existe.

---

## Estado

| | Vue | React |
| --- | --- | --- |
| Librería | Pinia | Zustand |
| Auth | `src/stores/auth.store.js` | misma ruta |
| Notificaciones | `src/stores/notification.store.js` | misma ruta |

Ambos vienen cableados: Pinia está instalado en la app, Zustand está en
`package.json`, y el store de auth persiste su token de la misma forma en que
`services/api.js` ya lo lee — una sola fuente de verdad, nombrada en un
comentario en el archivo.

**Cuándo no usar un store.** El estado que ningún otro componente lee pertenece
al componente. Un store es para algo que necesitan dos lugares no relacionados:
el layout lee el usuario, axios necesita el token. Eso es dos, así que es un
store.

---

## Claro y oscuro

Tres estados, no dos:

| `<html>` | Resultado |
| --- | --- |
| sin atributo | sigue al sistema operativo |
| `data-theme="dark"` | oscuro, diga lo que diga el sistema |
| `data-theme="light"` | claro, diga lo que diga el sistema |

El `dark:` de fábrica de Tailwind solo lee el sistema, lo que deja a quien
quiere el otro sin forma de decirlo. `src/style.css` redefine la variante para
revisar primero una elección explícita:

```css
@custom-variant dark {
  &:where([data-theme="dark"], [data-theme="dark"] *) { @slot; }
  @media (prefers-color-scheme: dark) {
    &:where(:root:not([data-theme="light"]), :root:not([data-theme="light"]) *) { @slot; }
  }
}
```

El `:not([data-theme="light"])` de la segunda mitad es la parte que vale
entender: sin eso, una elección explícita de claro pierde contra un sistema en
oscuro y el switch parece funcionar en una sola dirección.

**El toggle** es `ThemeToggle`, en el header de `LayoutAuthenticated`. Invierte
lo que está en pantalla y lo fija; "seguir al sistema" queda accesible con
`setTheme('system')` desde una pantalla de ajustes. La elección se guarda bajo
`<service>:theme`.

**El flash está resuelto.** `index.html` lleva doce líneas inline que leen la
elección guardada y ponen el atributo antes del primer pintado. Aplicar el tema
después del mount significa un frame del color equivocado en cada recarga, que
es el defecto más notorio que un switch de tema puede tener.

### Las superficies son tokens, no colores

```css
@theme inline {
  --color-surface: var(--ui-surface);   /* the page */
  --color-panel: var(--ui-panel);       /* cards, sidebar, header */
  --color-elevated: var(--ui-elevated); /* hover, skeletons, code */
  --color-line: var(--ui-line);         /* every border */
  --color-ink: var(--ui-ink);           /* primary text */
  --color-ink-soft: var(--ui-ink-soft); /* secondary */
  --color-ink-faint: var(--ui-ink-faint);
}
```

`inline` es lo que lo hace funcionar: las utilidades generadas emiten
`var(--ui-surface)` en vez de resolver en build time, así que redefinir siete
variables bajo `[data-theme="dark"]` da vuelta toda la interfaz.

Lo que eso compra se ve en los componentes: **ninguno lleva una clase `dark:`**.
`bg-panel` es correcto en los dos temas. Un componente que necesita un valor
claro y uno oscuro en cada línea no tiene un design system, tiene dos temas
hardcodeados que se desincronizan la primera vez que alguien edita uno.

La única excepción es el color de estado: `success`, `warning`, `danger`, `info`
en `BaseBadge` y `ToastHost`. Ahí el tono **es** el significado, así que no
puede salir de un token de superficie compartido. Esos se arman como un tinte al
10% de un solo tono más un `dark:` en el texto nada más, porque un tinte se lee
bien sobre cualquiera de las dos superficies y un `bg-emerald-50` sólido no.

`color-scheme` se define junto a los tokens, así que las barras de scroll, los
date pickers y el cursor de texto — que dibuja el navegador y nunca ve una
clase — combinan con el resto.

---

## Convenciones que los templates imponen

**Una sola instancia de axios.** `src/services/api.js` tiene la base URL, el
timeout, y un interceptor que convierte el `detail` RFC 7807 del backend en
`error.message`. Sin eso, cada componente muestra "Request failed with status
code 409" en vez de "Invoice INV-1 already exists".

**HTTP en `Services/`, nunca en componentes.** La página generada llama a
`listFacturas()`; ella maneja el estado de carga y de error, el service maneja
el request.

**Tailwind v4 con tokens.** Un `@import "tailwindcss"` y los bloques `@theme`
de `src/style.css`. Usa `bg-panel`, `border-line` y `text-ink` en vez de una
rampa neutra: un componente que nombra `zinc-200` directo es un componente al
que el tema no llega, y dos componentes que eligen rampas distintas son la
razón por la que una UI termina viéndose sutilmente mal sin que nada esté
identificablemente roto. Ver
[.jfast/skills/design-system/SKILL.md](../.jfast/skills/design-system/SKILL.md).

**Íconos sin runtime.** `@mdi/js` trae solo strings de path; `BaseIcon`
renderiza uno en un SVG. Sin icon font, tree-shaken a lo que importas.

Específico de Vue: `<script setup>` y la Composition API, nunca Options API;
componentes `PascalCase.vue`; las páginas a nivel de ruta terminan en
`View.vue`.

Específico de React: componentes función y hooks; componentes `PascalCase.jsx`;
las páginas a nivel de ruta terminan en `View.jsx`.

---

## Verificado cómo

Sé preciso con esto, porque importa para cuánto deberías confiar en ello.

**Probado en CI:** cada archivo renderiza; la estructura del módulo cae donde
debe; el router y el sidebar se parchan correctamente y de forma idempotente;
las `{{ }}` de Vue y las llaves de JSX sobreviven al scaffolding; el framework
se detecta desde el proyecto. Y, desde que aterrizaron los componentes base,
**`npm install` seguido de `npm run build` en ambos frontends generados** —
`scripts/smoke_components.sh`, que además verifica que `ToastHost` esté montado
en algún lado y que haya exactamente un store de toasts. Renderizar un template
prueba que las llaves estaban bien; no prueba que un componente importe algo
que existe.

**Ejercitado en un navegador real, una vez, a mano:** el switch de tema en los
dos frontends generados — claro, oscuro, una elección explícita de claro contra
un sistema en oscuro, la preferencia sobreviviendo una recarga, y el drawer
móvil abriendo. Los estilos computados se leyeron de vuelta, no se miraron a
ojo. Es una corrida en un navegador, no una suite: no está en CI y no va a
atrapar una regresión.

**No probado:** `npm run dev` como sesión interactiva, y nada más sobre cómo se
ve. Que el build pase significa que compila, no que un modal atrape el foco
correctamente con un lector de pantalla real.

---

## Angular

No se genera, a propósito. Un `angular.json` hecho a mano y una config de
builder que nunca corrió con `ng serve` es peor que no tener scaffold: parece
terminado y falla de una forma difícil de atribuir.

Si quieres Angular hoy: haz `ng new` del proyecto tú mismo, y después mantén la
misma estructura `Modulo<Name>` a mano. El soporte del generador es la fase 3
de PLAN.md, y debería aterrizar solo junto a un job de CI que realmente buildee
la salida.
