# Elegir datastores

Cada datastore es un plugin. Habilita lo que el servicio necesita, y el
cliente, el health check y el contenedor llegan todos juntos.

```toml
[plugins]
enabled = ["observability", "database", "cache", "qdrant", "mongo"]
```

| Plugin | Store | Provee | Extra | Offset de puerto |
| --- | --- | --- | --- | --- |
| `database` | PostgreSQL (+pgvector) | `db.engine`, `db.sessionmaker`, `db.databases` | `[db]` | +1 (y +2, +5, +6 para más instancias) |
| `cache` | Redis | `cache`, `cache.client` | `[cache]` | +3 |
| `mongo` | MongoDB | `mongo.client`, `mongo.db` | `[mongo]` | +4 |
| `qdrant` | Qdrant | `qdrant.client` | `[qdrant]` | +7 (HTTP), +8 (gRPC) |

Correr más de uno es normal. Los datos relacionales con foreign keys van en
PostgreSQL; los historiales de chat y los payloads scrapeados están más
cómodos en Mongo. El error es adoptar un segundo store antes de que el primero
deje de alcanzar — cada uno es una cosa más que respaldar, monitorear y
restaurar a las 3am.

---

## Instancias de base de datos con nombre

El plugin `database` guardaba un solo DSN. Ese único campo es la razón de que
no hubiera réplica de lectura, ni base de datos por tenant, ni shard — no eran
cuatro features faltantes, era una estructura faltante: una base de datos que
el servicio pueda **nombrar**.

```toml
[plugin.database.connections.primary]
dsn_env = "JFAST_DB_DSN"

[plugin.database.connections.replica]
dsn_env = "JFAST_DB_REPLICA_DSN"
read_only = true
pool_size = 4
```

**Omitir `connections` es exactamente lo mismo, escrito más corto.** Sin ese
bloque hay una sola instancia llamada `default`, configurada por los campos de
arriba y leyendo `JFAST_DB_DSN`. Todos los proyectos que ya existen, todos los
templates generados y `ctx.require("db.engine")` siguen funcionando sin cambiar
nada.

| Setting | Por conexión | Valor por defecto |
| --- | --- | --- |
| `dsn` / `dsn_env` | sí | `JFAST_DB_DSN`, si no `JFAST_DB_<NAME>_DSN` |
| `read_only` | sí | `false` |
| `pool_size`, `max_overflow`, `pool_timeout`, `pool_recycle`, `pool_pre_ping` | sí | el valor a nivel plugin |
| `include_infra`, `image`, `port_offset`, `database`, `user` | sí | el valor a nivel plugin |

`dsn_env` es el *nombre* de una variable, nunca un valor. Una conexión sin
`dsn` ni `dsn_env` lee `JFAST_DB_<NAME>_DSN`, así que una conexión `replica` no
necesita ninguna línea para encontrar `JFAST_DB_REPLICA_DSN`.

### Cuál es "la" base de datos

`db.engine` y `db.sessionmaker` siguen significando la instancia que se puede
escribir: la que se llama `default`, o `primary`, o la primera que no sea
`read_only`. Usa `default_connection` para decirlo explícitamente. Una
configuración donde todas las conexiones son `read_only` se rechaza al
arrancar — nada en ese servicio podría escribir, y enterarte en el primer
`POST` es enterarte tarde.

El mapa completo se publica como `db.databases`:

```python
databases = ctx.require("db.databases")
databases.names            # ("primary", "replica")
databases.engine("replica")
databases.sessionmaker()   # la instancia por defecto
```

`jfast describe --json` las lista, con la variable que lee cada una y nunca su
valor, más `max_connections` — la cantidad de conexiones al servidor que un
proceso puede abrir entre todas las instancias. Ese número es el que tiene que
entrar bajo el `max_connections` de PostgreSQL, y es el que la gente descubre
durante un incidente.

### Contenedores

Cada instancia con `include_infra` declara su propio contenedor, su propio
volumen y su propia variable de password:

```
postgres          8011:5432   POSTGRES_PASSWORD           postgres_data
postgres-replica  8012:5432   POSTGRES_REPLICA_PASSWORD   postgres_replica_data
```

Un password compartido haría que una filtración en cualquier lado fuera una
filtración en todos. Un servicio tiene diez puertos y los otros plugins ya
reclaman algunos (cache +3, mongo +4, qdrant +7 y +8, gRPC +9), así que las
bases de datos usan +1, +2, +5 y +6 — pide una quinta y el generador lo dice en
vez de chocar. Usa `include_infra = false` para una instancia administrada en
otro lado, que es el caso normal de una réplica en la nube.

Kubernetes no genera ninguna base de datos, a propósito (ver
[Kubernetes](kubernetes.md)), pero cada instancia enlazada llega al pod como su
propio `secretKeyRef`: `JFAST_DB_DSN` desde `db-dsn`, `JFAST_DB_REPLICA_DSN`
desde `db-replica-dsn`.

---

## Separación de lecturas y escrituras

```toml
[plugin.database]
read_write_split = true
```

Las lecturas van a una instancia `read_only`, las escrituras al primario:

```python
from jfastframework.plugins.builtin.database import (
    read_session_dependency,
    session_dependency,
)

@router.get("/invoices")
async def list_invoices(session = Depends(read_session_dependency)):
    ...

@router.post("/invoices")
async def create_invoice(session = Depends(session_dependency)):
    ...
```

Sin réplica configurada, `read_session_dependency` es la misma sesión que
`session_dependency`. Úsala en todos lados desde el principio y la separación
llega después como un bloque de configuración.

### La parte que no es opcional: el pin

**Una réplica va atrasada.** Guardas una fila, rediriges, lees de la réplica, y
la fila todavía no está. Es un 404 intermitente que aparece bajo carga y nunca
se reproduce en una laptop, porque una laptop no tiene réplica. Ningún flag lo
arregla: una separación sin pin es un bug que ya publicaste, no una feature.

Por eso, después de una escritura, las lecturas de ese cliente van al
**primario** durante `pin_window` segundos (5 por defecto).

**Cómo viaja el pin.** Como un token que lleva el cliente — una cookie
`jfast_rw` y un header `X-JFast-Read-Pin` — no como una entrada en una tabla de
este proceso. Un redirect puede caer en cualquier réplica del servicio, y un
pin que el siguiente proceso no puede ver es un pin que silenciosamente no
está. Los navegadores llevan la cookie gratis; un cliente de API que no guarda
cookies devuelve el header.

**Por qué confiar en un valor que controla el cliente es seguro acá.** La única
dirección en la que el cliente puede empujar es *hacia el primario*, que nunca
está desactualizado. El costo de un valor falsificado es capacidad del
primario, así que el valor se recorta a `pin_window` desde ahora: nadie puede
quedarse pineado para siempre.

**Qué dispara el pin.** Un método no seguro (`POST`, `PUT`, `PATCH`, `DELETE`)
que respondió por debajo de 400. Esto se tiene que decidir *antes* de que el
handler retorne: una sesión hace commit durante el teardown de la dependencia,
que corre cuando los headers de la respuesta ya salieron, así que el commit no
puede ser lo que setea la cookie. Para la escritura rara detrás de un `GET` —
un upsert perezoso, un contador — dilo:

```python
from jfastframework.plugins.builtin.database import mark_write

@router.get("/reports/{id}")
async def report(request: Request, session = Depends(session_dependency)):
    await touch_last_seen(session, id)
    mark_write(request)
```

La aproximación cuesta que un `POST` que no escribió nada pinee una ventana de
lecturas. Eso es carga, no incorrección, y `pin_on_unsafe_methods = false` lo
apaga para un servicio que marca sus escrituras a mano.

**Por qué cinco segundos, y qué sería mejor.** Un standby sano en la misma red
está a milisegundos; cinco segundos igual cubren un pico de checkpoint o un WAL
sender trabado, y pinear a un cliente cinco segundos después de escribir es
despreciable en una carga dominada por lecturas. La respuesta exacta es basada
en LSN: registrar `pg_current_wal_lsn()` en la escritura, compararlo con el
`pg_last_wal_replay_lsn()` de la réplica, y dejar de pinear en cuanto la
réplica alcanzó. Eso cuesta un round trip por lectura y solo funciona contra un
standby físico real, así que es el camino de mejora y no el default.

**Las escrituras no pueden llegar a una réplica.** Dos guardas, porque una de
ellas no puede ver SQL crudo. Una sesión de lectura se niega a terminar con
cambios ORM pendientes (`ReadOnlySessionError`), y toda conexión asyncpg
`read_only` setea `default_transaction_read_only = on`, así que un `INSERT`
colado por `session.execute(text(...))` lo rechaza PostgreSQL mismo.

### Sharding no es esto

Las instancias con nombre son la base *sobre* la que se construirá un mapa de
shards — resolución de claves, migraciones por shard y queries entre shards son
un trabajo aparte, y nada de eso era expresable mientras el plugin tenía un
solo DSN. No está construido.

Una base de datos por tenant sí: ver [Multi-tenancy](multitenancy.md).

---

## Cache

`get_or_set` es el camino de lectura. Todo lo demás en la fachada es una
primitiva a la que recurres cuando read-through no es la forma correcta.

```python
cache = ctx.require("cache")

report = await cache.get_or_set(
    f"report:{tenant_id}",
    lambda: build_report(tenant_id),   # cualquier corrutina sin argumentos
    ttl=300,
)
```

Hace tres cosas que `get` + `set` a mano no hacen.

**Sobrevive a que el cache esté caído.** Cualquier falla del backend dentro de
`get_or_set` degrada a llamar al loader. Un reinicio de Redis le cuesta a esas
requests una recomputación, no un 500. Esto es lo que vuelve cierto el
`health_critical=False` del plugin en vez de aspiracional — y es cierto *solo
en este camino*:

| Llamada | Redis inalcanzable |
| --- | --- |
| `get_or_set(...)` | devuelve el valor del loader |
| `get` / `set` / `delete` / `exists` / `publish` | lanza excepción |

Las primitivas lanzan a propósito. Un servicio que no distingue "no hay nada
cacheado" de "Redis desapareció" sirve respuestas viejas para siempre y nadie
se entera. Si las llamas directo en un camino de request, el `try/except` es
tuyo.

Los errores que lanza el *loader* siempre se propagan. Degradar ante una caída
del cache es el objetivo; degradar ante una query rota es cómo un servicio
devuelve respuestas incorrectas en silencio.

**Colapsa los misses concurrentes.** Cuando una key caliente expira bajo
carga, el read-through ingenuo manda todas las requests en vuelo a la base de
datos al mismo tiempo. Acá el primer caller que falla toma un lock corto en
Redis y recomputa; los demás consultan su resultado hasta `stampede_wait` y
después se rinden y cargan por su cuenta.

El trade, dicho sin vueltas: el lock cuesta un round trip extra en cada miss, y
un caller que pierde la carrera espera hasta `stampede_wait` antes de recurrir
al loader. Ese límite es lo que lo hace seguro — un loader trabado cuesta
trabajo duplicado, nunca una fila de requests trabadas.

La alternativa, recomputar temprano antes de que expire el TTL, se ahorra el
round trip pero necesita envolver cada valor en un sobre que cargue su expiry
lógico. Eso cambia lo que se guarda, y este Redis normalmente se comparte con
algo que no es un servicio JFast. Mantener los valores cacheados como
documentos JSON planos valía más que el round trip.

```toml
[plugin.cache]
stampede_wait = 2.0      # 0 apaga el lock por completo
stampede_lock_ttl = 10   # techo de cuánto puede retenerlo un caller
```

**Está contado.** Con el plugin `metrics` habilitado, el cache registra cuatro
counters en el registry compartido y `/metrics` los sirve junto con todo lo
demás:

| Counter | Significado |
| --- | --- |
| `cache_hits_total` | lecturas servidas desde el cache |
| `cache_misses_total` | lecturas que no encontraron nada guardado |
| `cache_errors_total` | operaciones que el backend rechazó |
| `cache_stampede_suppressed_total` | ejecuciones del loader evitadas esperando a otro caller |

`metrics` es una dependencia blanda (`after`, no `requires`): un servicio que
quiere un cache no debería tener que cargar `prometheus-client` para tenerlo.
Deshabilita `metrics` y los counters pasan a ser no-ops en silencio.

### TTL

`ttl=None` significa "usa `default_ttl`", así que no puede significar además
"nunca expira". `ttl=0` es esa vía de escape:

```python
await cache.set("feature-flags", flags, ttl=0)   # hasta que algo lo borre
```

Un `ttl` negativo lanza `ValueError` en el punto de llamada y no en el round
trip, para que el mensaje pueda nombrar la key.

### Dos cosas que parecen bugs y no lo son

**`publish` no aplica el prefijo de keys.** Todos los demás métodos
namespacean su key; los nombres de canal son un contrato con quien más esté en
este Redis — muchas veces una app Laravel que jamás oyó hablar de nuestro
prefijo. Renombrar el canal en silencio rompería justamente la interoperación
para la que existe el Redis compartido. Namespacear canales es tarea del
plugin `channels`, bajo su propio setting `prefix`.

**`get` devuelve el string crudo cuando el valor no es JSON.** Misma razón:
este servicio no es el único que escribe. Una key que escribió otro sistema
vale la pena devolverla como string, no vale la pena lanzar una excepción.

---

## Búsqueda vectorial: pgvector o Qdrant

El plugin `rag` habla contra un protocolo `VectorStore`, así que el backend es
una línea de config.

```toml
[plugins]
enabled = ["observability", "database", "rag"]

[plugin.rag]
store = "pgvector"       # the default
collection = "rag_chunks"
dimensions = 768
```

Cambiar a Qdrant:

```toml
[plugins]
enabled = ["observability", "qdrant", "rag"]

[plugin.rag]
store = "qdrant"
```

No cambia nada más. Los mismos endpoints, la misma forma de `SearchHit`, los
mismos scores — cada store normaliza a similitud coseno en [0, 1], así que
quien llama nunca tiene que saber si el backend devolvió una distancia o una
similitud.

### Cuál

| | pgvector | Qdrant |
| --- | --- | --- |
| Costo operativo | ninguno — es la base de datos que ya corres | un segundo servicio que correr y respaldar |
| Escala | cómodo hasta unos pocos millones de chunks | mucho más allá de eso |
| Filtrado | tenant id, y el SQL que escribas | filtros ricos sobre el payload, indexados |
| Cuantización | no | sí |

**Empieza con pgvector.** Pasa a Qdrant cuando choques con un muro específico
que puedas nombrar — complejidad de los filtros, tiempo de build del índice,
memoria. "Quizá escale mejor" no es ese muro.

### Elegir mal falla ruidosamente

Elegir `pgvector` sin el plugin `database`, o `qdrant` sin el plugin `qdrant`,
revienta al arrancar y con el arreglo en el mensaje:

```
rag store 'qdrant' needs the 'qdrant' plugin. Add "qdrant" to [plugins].enabled.
```

No en la primera búsqueda, en producción, un viernes.

---

## Un store propio

Implementa el protocolo — `ensure_schema`, `upsert`, `search`,
`delete_document`, `health` — y apunta la config ahí. La clase se construye
con el `AppContext`, así que puede sacar del registro de providers lo que
necesite:

```python
from jfastframework.vectors import Chunk, SearchHit


class WeaviateStore:
    def __init__(self, ctx):
        self._client = ctx.require("weaviate.client")

    async def ensure_schema(self) -> None: ...
    async def upsert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int: ...
    async def search(self, embedding, *, limit=5, tenant_id=None) -> list[SearchHit]: ...
    async def delete_document(self, document_id: str) -> None: ...
    async def health(self) -> tuple[bool, str]: ...
```

```toml
[plugin.rag]
store = "myapp.stores:WeaviateStore"
```

La misma escotilla de escape existe para los embedders
(`embedder = "myapp:OpenAIEmbedder"` — cualquier cosa con `dimensions` y un
`embed` async).

---

## Dimensiones del embedding

El error que cuesta una tarde: `dimensions` tiene que coincidir con el modelo
de embedding. `nomic-embed-text` es 768; `mxbai-embed-large` es 1024. Si no
coinciden falla al insertar, y cambiarlo después significa volver a embeber
cada documento que ya ingestaste. Decídelo antes del primer ingest.

---

## Enums: qué mitad de la garantía estás comprando

`Enum` parece una decisión y son dos. Las dos respuestas importan, y los
defaults no te dan el par que la mayoría asume.

```python
class Status(enum.Enum):
    pending = "pending"
    done = "done"


status: Mapped[Status] = mapped_column(Enum(Status, native_enum=False))
```

Esa columna es un `VARCHAR` común. **No se crea ningún `CHECK`.**
`create_constraint` viene en `False` desde SQLAlchemy 1.4, y `native_enum=False`
solo apaga el tipo `ENUM` de PostgreSQL — no pone nada en su lugar. Nada fuera
de la aplicación frena un valor inválido:

```sql
-- with native_enum=False and nothing else
CREATE TABLE t (s VARCHAR(7))
```

Las tres opciones, y lo que cuesta cada una:

| | Almacenamiento | Se puede escribir un valor inválido | Agregar un miembro |
|---|---|---|---|
| `Enum(Status)` (default) | tipo nativo `status_enum` | no — lo rechaza el servidor | `ALTER TYPE ... ADD VALUE`, una migración |
| `Enum(Status, native_enum=False)` | `VARCHAR(n)` | **sí** — desde cualquier cliente que no sea el ORM | nada; deployas el código |
| `Enum(Status, native_enum=False, create_constraint=True)` | `VARCHAR(n)` + `CHECK` | no | una migración que reescribe el `CHECK` |

```sql
-- with create_constraint=True
CREATE TABLE t (s VARCHAR(7), CONSTRAINT status_enum CHECK (s IN ('pending', 'done')))
```

**Usa `native_enum=False` sin constraint** para un conjunto que todavía se
mueve — estados, tipos, cualquier cosa que cambie por una decisión de producto.
Agregar un miembro es un deploy de código y nada más, que es toda la razón para
resignar el tipo nativo. A cambio, acepta que una sesión de psql, un `COPY`
masivo u otro servicio sobre la misma base pueden escribir `"pendign"` y la
columna lo va a tomar. Valida en el borde, donde llega el valor, y trata los
valores desconocidos como un caso real al leer — un `ValueError` saliendo de
`Status(row.status)` es un 500 que parece un bug en el lugar equivocado.

**Agrega `create_constraint=True`** cuando algo que no es este servicio escribe
la tabla, o cuando un valor incorrecto es un problema de corrección y no de
presentación. Pagas una migración por miembro, igual que con el tipo nativo —
pero un `CHECK` es más barato de cambiar que un `ENUM` de PostgreSQL, que ni
siquiera puede eliminar un valor.

Ten en cuenta que autogenerate no detecta de forma confiable los cambios de
miembros de un enum, en ninguna de las dos direcciones. Elijas la que elijas,
esa migración se escribe a mano.

---

## Deploy

Los plugins de datastore habilitados aportan sus contenedores al archivo de
compose generado, automáticamente:

```bash
jfast deploy compose --stdout
```

Deshabilita `cache` y el contenedor de Redis desaparece de la siguiente
generación. Ese es el punto de derivar la infraestructura del grafo de plugins
en vez de mantenerla en paralelo.

Los secretos se quedan en el entorno. El compose generado los referencia con
la forma fail-fast de compose, así una contraseña faltante detiene el stack en
vez de arrancar PostgreSQL abierto de par en par:

```yaml
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}
```
