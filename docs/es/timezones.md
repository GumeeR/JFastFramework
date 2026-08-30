# Zonas horarias

El almacenamiento quedó resuelto en `0.1.0a4`: `UTCDateTime` escribe UTC, lee
UTC, y rechaza un datetime naive al escribir. Esta página trata la otra mitad —
**calcular** con esos valores — que es donde un deploy multi-región falla de
verdad.

## La decisión

**Nada se guarda en hora local. Guarda UTC, renderiza local.**

No es una preferencia. Un timestamp local es *ambiguo* durante una hora cada
otoño e *imposible* durante una hora cada primavera, y ningún tipo de columna
registra cuál de las dos era una fila dada. Ordénalo, réstalo, o replícalo a una
máquina en otro país y no hay forma de recuperar qué quiso decir quien lo
escribió. UTC no tiene ninguno de los dos problemas: cada instante tiene
exactamente una representación, y cada representación nombra exactamente un
instante.

Si alguien necesita la hora local **de cuando ocurrió algo** — no la de ahora —
eso es una columna aparte con el nombre de la zona al lado:

```python
closed_at: Mapped[datetime] = mapped_column(UTCDateTime)   # the instant
closed_at_zone: Mapped[str]                                # "America/Santiago"
```

Nunca un segundo timestamp con el mismo momento en hora local. Dos timestamps
para un evento son dos oportunidades de equivocarse y ninguna forma de saber
cuál es la equivocada.

## El fallo que esto evita

```sql
SELECT date_trunc('day', created_at), sum(total) FROM orders GROUP BY 1
```

`date_trunc` calcula en el `TimeZone` de la **sesión**, que por defecto es el del
servidor. Dos réplicas de los mismos datos, configuradas distinto, responden
distinto — y ninguna lanza un error, escribe un log ni se ve mal:

```
row stored: 2026-03-01T02:30:00+00:00, identical in both cases

TimeZone=America/Santiago   date_trunc -> 2026-02-28   ::date -> 2026-02-28
TimeZone=UTC                date_trunc -> 2026-03-01   ::date -> 2026-03-01
```

Otro día, otro mes, la misma fila. `CURRENT_DATE`, `now()::date`,
`localtimestamp` y todo `AT TIME ZONE` sin zona explícita se comportan igual.
Nadie se da cuenta hasta que alguien concilia dos reportes.

## Tres zonas, y no son la misma zona

| | Qué es | Quién la define |
| --- | --- | --- |
| **Zona de almacenamiento** | UTC. Siempre. | Nadie — no es configurable |
| **Zona de sesión** | En qué calcula la base de datos | `[plugin.database] session_timezone`, fijada a UTC |
| **Zona de negocio** | Qué significa "hoy" para un reporte | `[app] timezone` |
| **Zona del tenant** | La zona de negocio de un tenant | `[plugin.tenancy.timezones]` |

La zona del servidor está deliberadamente ausente de esa tabla. Es un accidente
de dónde corre el contenedor y nunca se le permite decidir una respuesta.

## La zona de sesión queda fijada a UTC

Cada engine que abre el plugin `database` lleva `timezone=UTC` como **parámetro
de arranque** de asyncpg:

```toml
[plugin.database]
session_timezone = "UTC"   # the default
```

Un parámetro de arranque y no un `SET` después de conectar, a propósito. Una
conexión del pool se entrega a mitad de su vida, así que una sentencia que corrió
una sola vez al abrir el socket está a un `DISCARD ALL`, un `RESET ALL` o un
server-reset de pgbouncer de desaparecer — y la sesión vuelve entonces a la zona
del servidor sin dejar rastro. Un parámetro de arranque es parte de lo que la
conexión *es*, y asyncpg lo reenvía al reconectar.

Esto aplica igual a las conexiones con nombre, a las réplicas y a los engines por
tenant: una base de datos de tenant es una base de datos como cualquier otra, y
el reporte que responde no puede depender de en qué servidor cayó ese tenant.

Poner `session_timezone = ""` desactiva la fijación y deja la configuración del
servidor intacta. Eso solo es correcto cuando algo fuera del servicio ya la
garantiza.

Solo los DSN `postgresql+asyncpg` reciben el parámetro — `server_settings` es un
argumento de asyncpg y otro driver lo rechazaría. SQLite no tiene zona del lado
del servidor que fijar.

## `[app] timezone` — la zona de negocio

```toml
[app]
timezone = "America/Santiago"
```

Esto es lo que responde "qué día es" para un reporte, un período de facturación
o una cuota diaria. Por defecto `"UTC"`. El nombre se valida contra `zoneinfo`
**al arrancar**, no en el primer request que formatea una fecha:

```
ValidationError: unknown time zone 'America/Santaigo'. Use an IANA name such as
'America/Santiago' or 'UTC'. ...
```

Un typo acá es invisible de otro modo hasta que un total de cierre de mes cae en
el día equivocado.

## `jfastframework.time`

El módulo chico que todos los proyectos reescriben mal. Es chico a propósito: se
gana su lugar por ser correcto, no por ser amplio.

```python
from jfastframework.time import now, today, day_bounds, in_zone, parse

now()                       # aware, UTC, always
today()                     # the local date in the business zone
today("America/Santiago")   # the local date in a named zone
day_bounds(today())         # the UTC half-open range of that local day
in_zone(value, tz)          # render an instant; refuses naive input
parse("2026-03-01T12:00:00-03:00")   # aware UTC; refuses a string with no offset
```

### `day_bounds` es el que importa

"Los pedidos de hoy" es una pregunta de zona horaria. La versión naive — de
medianoche a medianoche en UTC — está mal por el offset en **ambos** extremos:
cuenta parte de ayer y se pierde el final de hoy.

```python
start, end = day_bounds(date(2026, 3, 1), "America/Mexico_City")
# 2026-03-01T06:00:00+00:00 .. 2026-03-02T06:00:00+00:00
```

```python
rows = await session.execute(
    select(Order).where(Order.created_at >= start, Order.created_at < end)
)
```

Semiabierto, nunca `BETWEEN`. Un límite superior cerrado o cuenta la medianoche
dos veces o se come el último microsegundo, y cuál de los dos depende de la
precisión de la columna.

### Un día local no siempre dura 24 horas

Esta es la mitad que se rompe, y la que vale la pena testear. Números reales,
sacados de `tests/test_time.py`:

| Día local | Zona | Rango UTC | Duración |
| --- | --- | --- | --- |
| 2026-09-06 | America/Santiago | `04:00Z` → `2026-09-07T03:00Z` | **23 h** |
| 2026-04-04 | America/Santiago | `03:00Z` → `2026-04-05T04:00Z` | **25 h** |
| 2021-04-04 | America/Mexico_City | `06:00Z` → `2021-04-05T05:00Z` | **23 h** |
| 2021-10-31 | America/Mexico_City | `05:00Z` → `2021-11-01T06:00Z` | **25 h** |
| 2026-03-01 | America/Mexico_City | `06:00Z` → `2026-03-02T06:00Z` | 24 h |

Dos zonas porque se rompen de forma distinta:

- **Santiago** cambia la hora a las **00:00**. El 2026-09-06 el reloj salta de
  `00:00` directo a `01:00`, así que *la medianoche local no existe ese día*.
  `day_bounds` resuelve ambos extremos con `fold=0`, que lee una hora local
  inexistente a través del offset previo a la transición y cae exactamente en el
  instante de la transición — el verdadero primer instante de ese día local.
  `fold=1` daría `03:00Z`, una hora *antes* de que el día empezara.
- **Ciudad de México** cambiaba la hora a las **02:00** hasta que abolió el
  horario de verano en 2022. La medianoche existe; la hora se agrega o se pierde
  dentro del día. Y la fila de 2026 es el punto: la misma fecha de calendario que
  se habría movido en 2021 hoy no se mueve, porque el offset se lee de tzdata en
  vez de recordarse.

Los días consecutivos se tocan exactamente una vez en todas esas transiciones —
sin hueco y sin solapamiento, así que un instante pertenece a exactamente un día
local.

## Zonas por tenant

Un solo deploy, tenants en países distintos, cada uno viendo sus propios límites
de día:

```toml
[plugin.tenancy.timezones]
acme = "America/Santiago"
globex = "America/Mexico_City"
```

```python
from fastapi import Depends
from jfastframework.plugins.builtin.tenancy import tenant_zone
from jfastframework.time import day_bounds, today

@router.get("/reports/today")
async def report(tz = Depends(tenant_zone)):
    start, end = day_bounds(today(tz), tz)
    ...
```

**Un tenant sin entrada usa `[app] timezone`**, que por defecto es UTC — nunca la
zona del servidor, porque eso es justamente lo que hace que una respuesta dependa
de dónde corre el contenedor. El mismo fallback aplica a un request que no
resolvió ningún tenant, que es lo que son los health checks, `/metrics` y la
documentación.

Cada nombre de la tabla se valida al arrancar. Un typo detiene el servicio en vez
de correr los reportes de un tenant por un día.

El mapa es configuración estática. Un servicio que guarda las zonas de sus
tenants en una base de datos escribe `request.state.tenant_timezone` desde su
propia dependencia o middleware; `tenant_zone` lee ese atributo y solo cae al
fallback cuando no está.

## El check del contrato

`datetime.now()` sin argumento y `datetime.utcnow()` son donde nacen los valores
naive. Que `UTCDateTime` rechace uno al escribir es la red de seguridad — y una
red de seguridad te dice que una fila estaba mal, no qué línea la escribió. Por
eso el checker los rechaza:

```
service.py:12: naive-datetime: datetime.datetime.now() returns a datetime with
no time zone  (it reads as local time to whoever renders it, and UTCDateTime
refuses it on write. Use datetime.now(UTC), or jfastframework.time.now())
```

Se reportan: `datetime.now()` (sin `tz`), `datetime.utcnow()`,
`datetime.utcfromtimestamp()`. No se reportan: `datetime.now(UTC)`,
`datetime.now(tz=zone)`, ni `datetime.now(*args)` — la sintaxis no puede decir
qué contiene un splat, y un checker que adivinara se equivocaría con total
seguridad.

Se puede eximir en línea como cualquier otro hallazgo:

```python
return datetime.now()  # contracts: allow wall clock, for display only
```

La regla vive en `[rules.async_safety]` — la única tabla de reglas que tenía el
modelo de contrato — pero tiene su propio switch, así que silenciarla no
silencia con ella el chequeo del event loop:

```toml
[rules.async_safety]
enabled = true
naive_datetime = false   # naive-datetime apagada, async-blocking sigue encendida
```

Medido sobre un servicio generado con un `time.sleep()` y un `datetime.now()`
dentro de `async def`, y los dos otra vez en un método async:

| Configuración | `jfast contracts check` | Exit |
| --- | --- | --- |
| defaults | 4 violaciones: 2 `async-blocking`, 2 `naive-datetime` | 5 |
| `naive_datetime = false` | 2 violaciones: solo `async-blocking` | 5 |
| `enabled = false` | ninguna | 0 |

`enabled = false` sigue apagando las dos. Ese es el switch que conviene conocer
antes de que alguien lo use — `naive_datetime = false` es el angosto.

## `zoneinfo` y contenedores slim

`zoneinfo` es biblioteca estándar desde 3.9, así que no hay dependencia nueva. En
Linux lee la tzdata **del sistema** en `/usr/share/zoneinfo` — incluso para la
clave `"UTC"`.

Una imagen construida `FROM python:3.12-slim` sin `tzdata` instalada lanza
entonces `ZoneInfoNotFoundError` para **todos** los nombres. Dos consecuencias:

- `"UTC"` acá resuelve a `datetime.UTC`, un offset fijo sin ningún archivo
  detrás. Un servicio que nunca nombra una zona real funciona en una imagen que
  no tiene ninguna.
- Nombrar una zona real en esa imagen falla **al arrancar**, con un mensaje que
  dice qué paquete falta.

Se arregla de cualquiera de las dos formas:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends tzdata
```

```toml
dependencies = ["tzdata"]   # the Python package, if you would rather not touch the image
```

**No** pongas la variable `TZ` del contenedor y lo des por resuelto. Eso cambia
lo que devuelve `datetime.now()`, que este framework no lee, y deja intacta la
zona de sesión de la base de datos — que es lo que realmente decide el reporte.

## `jfast doctor`

`doctor` reporta el `TimeZone` de la base de datos y avisa cuando no es UTC. El
aviso no es por este servicio, que fija sus propias sesiones: es por todos los
*otros* clientes de esa base de datos — `psql`, una herramienta de BI, una
migración corrida a mano — que siguen calculando días en la zona del servidor.
