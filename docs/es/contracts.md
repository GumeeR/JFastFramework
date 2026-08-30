# Contratos

`AGENTS.md` dice qué hacer. Un contrato dice qué está **permitido**, y algo lo
verifica.

Esa diferencia es todo el punto. Un agente que genera código a toda velocidad
se va a pasar de largo una sugerencia sin notarlo — no por mala fe, sino
porque nada lo frenó. Un contrato lo frena:

```bash
jfast contracts check
```

```
modules/invoice/repository.py:1: layer-package: 'storage' must not import 'fastapi'  (Data access. No business rules.)

1 violation(s). Fix them, or waive one inline with
    # contracts: allow <reason>
```

Salida distinta de cero. En CI, eso es un build roto.

---

## Las tres audiencias, un archivo

`contracts.toml` está en la raíz de cada servicio generado.

| Audiencia | Lo lee como |
| --- | --- |
| El build | `jfast contracts check` — falla ante una violación |
| Un agente | `jfast contracts show --json` — antes de escribir una línea |
| Una persona | `CONTRACTS.md` — generado, para review |

Una fuente, tres representaciones, para que el documento y la regla aplicada
no puedan contradecirse.

---

## Lo que declaras

### Alcance

```toml
[project]
name = "billing"
owns = "Invoices and payments."
does_not_own = "Customers. Ask the catalog service."
```

`does_not_own` es la mitad más útil, y la que la gente omite. Casi todo el
código malo en un sistema que crece es un servicio expandiéndose en silencio
hacia algo que otro servicio ya posee — y nunca se ve mal desde adentro de ese
servicio.

### Capas

```toml
[layers.domain]
description = "Entities and their rules. Framework-free."
paths = ["modules/*/[!_]*.py"]
may_import = ["shared"]
forbid_packages = ["fastapi", "sqlalchemy", "pydantic"]

[layers.http]
paths = ["modules/*/http.py"]
may_import = ["use_cases", "domain", "shared"]
```

`may_import` nombra **otras capas**, no paquetes. Las capas son sobre lo que
discute un reviewer; los nombres de paquetes son lo que se le olvida.

Un archivo se clasifica por el patrón coincidente **más específico** — el de
menos comodines, no el de cadena más larga. Esa distinción es crítica:
`modules/*/[!_]*.py` es más largo que `modules/*/http.py`, y ordenar por
longitud clasificaría cada router como código de dominio para después rechazar
sus imports por una razón que nadie podría deducir.

#### `shared` está en todas las listas, y en ninguna propia

Todo contrato generado declara una capa `shared` para `shared/*.py`, y todas
las demás capas pueden importarla — el dominio incluido. No es un aflojamiento:
es lo que hace legal el consejo de [shared/, enums y
channels](shared-and-events.md). `[rules.placement]` te dice que muevas a
`shared/` el enum que quiere un segundo módulo, y una capa que no podía
importar `shared/` no tenía forma de obedecer la instrucción que el propio
checker imprimía.

Sigue siendo seguro porque `shared` mantiene `may_import = []` y se prohíbe a
sí misma `sqlalchemy` y `fastapi`. Nada llega a una base de datos ni a un
router a través de un enum, y la dirección sigue siendo de una sola vía — lo
que `[rules.placement]` verifica desde el otro lado.

### Llamadas prohibidas

```toml
[[rules.forbid_call]]
pattern = "os.getenv"
except_in = ["settings.py", "config/*.py", "migrations/env.py"]
why = "Configuration is typed. Add a field to a settings model so a bad value fails at boot."
```

`why` no es decoración. Es lo que imprime el checker, y la diferencia entre
alguien que arregla la causa y alguien que borra la línea.

Un patrón con puntos también atrapa el import pelado, así que
`from os import getenv` no se escapa. Un patrón sin puntos coincide exacto,
así que prohibir `print` no marca además `report.print()`.

### Estructura requerida

```toml
[[rules.require]]
path = "tests"
applies_to = "modules/*"
why = "A module with no tests is a module nobody can change safely."
```

### Seguridad del event loop

El bug más difícil de un servicio async es el que nunca lanza una excepción.
Una llamada bloqueante dentro de `async def` frena todas las demás requests de
ese worker mientras dura, y el síntoma llega como latencia en endpoints que no
tienen nada que ver con la causa. Nada en el traceback, nada en el log, y un
profile del endpoint lento apunta a código que es inocente.

Así que es una regla de contrato, verificada en cada build:

```toml
[rules.async_safety]
enabled = true
allow_in = ["tests/*", "conftest.py", "scripts/*", "migrations/*"]
follow_local_helpers = true

[rules.async_safety.extra_blocking]
"myapp.legacy.render_pdf" = "await asyncio.to_thread(render_pdf, ...)"
```

```
blocking_demo.py:14: async-blocking: requests.get() blocks the event loop inside async send()
  (every other request on this worker waits. Use httpx.AsyncClient, already a dependency of gateway and auth)
blocking_demo.py:13: async-blocking: warm_cache() is synchronous and calls time.sleep(), which blocks the event loop
  (make the helper a coroutine, or offload it with asyncio.to_thread)
```

Tres cosas que encuentra y que un linter de propósito general no:

* **Clientes en `self`.** `self._s3 = boto3.client("s3")` en `__init__`, y
  después `self._s3.put_object(...)` en un método async cuatro pantallas más
  abajo. La llamada es un método sobre una instancia, invisible para una regla
  que lee una función a la vez.
* **Un salto de indirección.** La llamada bloqueante casi nunca está en el
  handler; está en el helper síncrono que el handler llama. Dentro de un
  archivo, ese helper se sigue hasta quienes lo llaman.
* **Tu propio código.** `extra_blocking` es donde el equipo anota las
  funciones que solo él conoce, con el reemplazo al que hay que ir.

Conoce `boto3`, `pymongo`, `psycopg2`, el cliente síncrono de `redis` y
`sqlite3` por nombre, más los casos de la biblioteca estándar: `time.sleep`,
`subprocess`, `requests`, I/O bloqueante de `pathlib`, `open()`, y
`asyncio.run` o `run_until_complete` dentro de una corrutina.

**Lo que no va a hacer**, por el mismo principio que el resto del checker:

* Un handler con `def` síncrono no se reporta. FastAPI lo corre en un
  threadpool; esa es una forma soportada de escribir una ruta, no un bug.
* El trabajo entregado a `asyncio.to_thread`, `run_in_executor`,
  `anyio.to_thread.run_sync` o `run_in_threadpool` es código correcto y se
  deja en paz -- incluida la clausura síncrona que le pasas, que es por lo que
  `storage/s3.py` no reporta nada.
* Una llamada que no puede resolver a través de los imports del archivo no se
  reporta. `self._client.ping()` podría ser cualquier cosa, y un checker que
  adivinara marcaría cada `ping` del codebase.
* Nada cruza el límite de un archivo. Resolver un nombre hasta su definición
  en otro módulo es trabajo de un type checker.

Si además usas ruff, activa su ruleset `ASYNC` -- cubre los casos de la
biblioteca estándar de forma independiente. Este framework lo hace, y prender
la regla encontró dos llamadas bloqueantes a `Path.is_dir()` en su propia
sonda de readiness.

Exime una cuando bloquear de verdad es lo correcto:

```python
time.sleep(0)  # contracts: allow one-off at startup, not per request
```

### Interfaces

```toml
[[provides]]
name = "invoices-api"
kind = "http"
path = "/invoices"
stability = "stable"      # experimental | stable | deprecated

[[consumes]]
name = "catalog"
via_env = "API_CATALOG_URL"
```

Anotado para que cambiar una interfaz `stable` sea una decisión visible en vez
de una sorpresa para quien haya dependido de ella.

### Invariantes

```toml
[invariants]
rules = [
  "Money is stored in minor units as an integer. Never a float.",
  "A job handler is idempotent: delivery is at-least-once.",
]
```

El checker no puede verificarlas. Están aquí justamente porque nada más las va
a atrapar — esta es la lista que un reviewer, o un agente, revisa a mano.

---

## Exenciones

```python
from sqlalchemy import text  # contracts: allow one-off reporting query, JF-412
```

La razón es obligatoria. Una exención es una decisión; `jfast contracts
waivers` lista cada una, porque las decisiones que nadie revisa son
exactamente cómo un contrato deja de significar algo.

---

## Por qué existe una regla: `contracts explain`

`contracts check` dice que se rompió una regla. No dice *por qué la regla está
ahí*, y un agente al que le llega una violación sin remedio tiende a satisfacer
al checker en vez de arreglar el diseño — borrando el import, copiando el
código al segundo módulo, o apagando la regla. `explain` cierra eso:

```bash
jfast contracts explain billing analytics          # may billing import analytics?
jfast contracts explain http sqlalchemy            # may the http layer import it?
jfast contracts explain --rule shared-direction    # what is that rule, and where
jfast contracts explain --file modules/billing/service.py
jfast contracts explain --json
jfast contracts explain                            # every rule that can fire here
```

```
may module 'invoice' import module 'customer'?  [FORBIDDEN]

  rule     cross-module  -- one module imported another module
  what     modules may not import each other: 'invoice' and 'customer' would become one module
           with a folder between them
  declared contracts.toml:110
           [rules.placement]
  why      Two modules that import each other are one module with a folder between them. Neither
           can be extracted into a service later, and a change to one breaks the other in a way
           no test covers. So when a second module needs the same enum, type or pure function,
           it moves to shared/ -- and the check names the file.
  instead  - move what both modules need into shared/models.py, then import it from both
           - if only 'invoice' needs it, it belongs in 'invoice'
           - waive this one line with # contracts: allow <reason> while the move is in flight

  waiver   # contracts: allow <reason> -- one line, and only the rule that fired on it
           jfast contracts waivers lists every one, so it stays a reviewable decision
           deleting the rule from contracts.toml removes it for every file and for everyone,
           silently, and nothing reports the next violation
```

Cuatro cosas, y la segunda es la que no daba nada más:

* **Qué regla** lo prohíbe, en el mismo vocabulario que imprime `check`.
* **Dónde está declarada** — `contracts.toml` y un número de línea, con la
  línea misma, así la afirmación se verifica en vez de creerse. Cada layout
  declara capas distintas en líneas distintas, y la respuesta sigue al contrato
  que tiene enfrente.
* **Por qué está la regla**, citado del comentario que su autor escribió arriba
  de esa declaración. Los contratos generados llevan una justificación arriba
  de cada regla; esto la lee en vez de inventar prosa. Donde no hay comentario,
  usa el `description` de la capa y el `why` de la regla.
* **Qué hacer en su lugar**, nombrando un destino — `shared/models.py`, no "no
  hagas eso" — y **qué cuesta una exención**, sus dos mitades: la exención
  inline ocupa una línea y queda listada por `jfast contracts waivers`,
  mientras que editar `contracts.toml` saca la regla para todos, en silencio.

### Cuando no puede responder

Una respuesta que no puede derivar se reporta como `[UNKNOWN]` con lo que sí
sabe — las capas que declara el contrato, los módulos en disco — y el comando
sale con código distinto de cero, así un script distingue "no" de "no sé". Lo
mismo aplica a un archivo que ninguna capa reclama: eso no es un aprobado, es
un path que nadie incluyó.

`--json` lleva todo eso, más los `paths`, `may_import`, `forbid_packages` y
`description` de cada capa, y las violaciones vivas del archivo por el que
preguntas. Está pensado para ser lo bastante completo como para que un modelo
nunca tenga que abrir `contracts.toml` — porque un modelo que lo abre está a un
edit de borrar la regla.

`--contract PATH` apunta a un `contracts.toml` (o al directorio que lo
contiene) cuando el comando no corre desde adentro del servicio.

---

## Qué cambió estructuralmente: `contracts diff`

```bash
jfast contracts diff
jfast contracts diff --json
```

```
Architecture changes  (billing)

  + invoice -> customer           cross-module  modules/invoice/enums.py:33
                                  modules may not import each other
  - http -> shared                permitted, and no import uses it  declared at contracts.toml:32
  - storage -> schemas            permitted, and no import uses it  declared at contracts.toml:47

Potential breaking change:
  invoice loses access to customer.Customer when that import goes -- move it to shared/models.py
    and import it from both
```

**No es un diff de git, y el límite vale decirlo claro.** Nada de esto lee una
revisión anterior ni sabe cómo estaba el código ayer. Compara la arquitectura
que `contracts.toml` *permite* contra los imports que el código *hace*:

* `+` es una arista que el código tiene y el contrato no permite — el mismo
  hallazgo que reporta `contracts check`, dicho como cambio de arquitectura,
  con los símbolos que cruzan la arista.
* `-` es una arista que el contrato permite y ningún import usa: un permiso que
  se podría ajustar, no algo que se haya quitado.
* **Potential breaking change** lista lo que cuesta hacer cumplir el contrato:
  qué nombre pierde el importador si esa arista se va, y a dónde moverlo. Esa
  es la diferencia entre mover el código y borrar el import.

Solo cuentan los imports estáticos, y solo entre capas declaradas y directorios
bajo `modules/`. Es solo reporte — siempre sale con cero; al build lo hace
fallar `contracts check`.

---

## Lo que el checker deliberadamente no hace

Es estático, basado en AST y conservador. Un checker que grita lobo consigue
un archivo de ignore en una semana, y ahí el contrato vuelve a ser decoración.

- **Los archivos que no coinciden con ninguna capa no se verifican por capa.**
  Un path se incluye *explícitamente*. Adivinar produciría ruido en cada
  script, migración y notebook.
- **Solo se inspeccionan imports estáticos y llamadas directas.** `importlib`
  y las cadenas de `getattr` quedan fuera de alcance. Esto es una barandilla
  de diseño, no un sandbox.
- **El contrato se valida primero.** Dos capas reclamando un mismo path, o un
  `may_import` que nombra una capa que no existe, se reportan como errores del
  contrato — porque si no, los hallazgos son respuestas seguras a la pregunta
  equivocada.

---

## Usarlo con un agente

Pon esto en las instrucciones del agente, o apóyate en `AGENTS.md`, que ya lo
hace:

> Antes de escribir código aquí, corre `jfast contracts show --json`. Antes de
> dar el trabajo por terminado, corre `jfast contracts check`. Cuando reporte
> una violación, corre `jfast contracts explain --rule <rule> --json` antes de
> cambiar nada — y nunca edites `contracts.toml` para que un check pase.

El JSON lleva alcance, límites de capas, llamadas prohibidas, interfaces e
invariantes. Con eso alcanza para que un agente escriba código que encaja a la
primera, en vez de código que un reviewer tiene que devolver.

Y cuando igual se desvía, el check lo atrapa — que es la parte que hace a esto
distinto de escribir las mismas reglas en prosa y confiar.

---

## Comandos

```bash
jfast contracts init                    # defaults for your layout
jfast contracts init --layout screaming
jfast contracts check                   # non-zero exit on a violation
jfast contracts check --json
jfast contracts show --json             # what an agent reads first
jfast contracts render                  # CONTRACTS.md
jfast contracts waivers                 # every inline exception
jfast contracts explain <a> <b>         # why that import is refused, and what to do
jfast contracts explain --rule layer    # what a reported rule means, and where it lives
jfast contracts diff                    # permitted architecture vs. the built one
```

Agrégalo al CI del servicio, al lado de los tests:

```yaml
- run: jfast contracts check
```

## Una advertencia que vale la pena decir

Un contrato atrapa el desvío estructural: una capa alcanzando para el lado
equivocado, una llamada prohibida, un directorio de tests que falta. No atrapa
un algoritmo equivocado, un mal nombre, o una regla implementada al revés.

Hace que el código generado sea *estructuralmente* limpio y hace explícitas
las reglas. No hace que el código sea correcto. El review sigue aplicando — el
contrato solo saca las discusiones que si no tendrías cada vez.
