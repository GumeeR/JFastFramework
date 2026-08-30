# Workspaces y el API gateway

Un solo servicio no necesita workspace. En cuanto hay dos, aparecen tres
preguntas que la configuración por servicio no puede responder:

* qué puertos ya están tomados;
* a qué debe llamar el frontend;
* si hay algo puesto adelante de ellos.

`jfast.workspace.toml` responde las tres, y es el único archivo que conoce el
sistema entero.

---

## Recursos: datastores con nombre

Antes, un workspace registraba los datastores de un servicio por *tipo*:

```toml
datastores = ["database", "cache"]
```

De ahí salían dos cosas, y las dos eran bugs disfrazados de diseño. El archivo
de compose generado creaba un contenedor `billing-database` y **nada escribía
el DSN que apuntaba a él** -- la cadena de conexión quedaba mantenida a mano en
un `.env` al lado de un contenedor que sí era generado, que es exactamente
donde vive el drift. Y un segundo PostgreSQL no se podía expresar de ninguna
forma, porque no había un nombre del cual colgar la segunda instancia.

Un recurso tiene nombre. Un servicio se enlaza a él bajo una variable.

```toml
[[workspace.resources]]
name = "core-db"
type = "postgres"
port = 8900
database = "core"

[[workspace.resources]]
name = "shared-redis"
type = "redis"
port = 8901

[[workspace.services]]
name = "billing"
kind = "api"
port = 8010
path = "billing"
uses = [
  { resource = "core-db", as = "JFAST_DB_DSN" },
  { resource = "shared-redis", as = "JFAST_CACHE_URL" },
]
```

`as` toma su valor por defecto del tipo -- un recurso `postgres` cae en
`JFAST_DB_DSN`, que es lo que lee `DatabaseSettings` -- así que el caso común
no necesita configuración para ser correcto.

### Los comandos

```bash
jfast workspace resource analytics-db --type postgres --database analytics
jfast link billing analytics-db --as JFAST_ANALYTICS_DSN
jfast link catalog shared-redis
jfast unlink catalog catalog-cache
jfast workspace resource catalog-cache --remove
jfast workspace validate
jfast workspace env          # writes every .env from the bindings
jfast workspace compose      # one container per resource
jfast workspace graph        # mermaid, edges labelled with the variable
```

Los tipos son `postgres`, `redis`, `mongo` y `qdrant`. Los recursos toman
puertos de una banda que arranca en `base_port + 900`, por encima del puerto
base y por debajo del primer bloque de servicio, así que un recurso nunca puede
caer dentro del bloque de diez puertos de un servicio.

### Contenedores que son de un plugin

Esos cuatro tipos son los datastores que el workspace nombra y comparte. Todo
lo demás que un servicio necesita viene de sus plugins: `events` es dueño de un
broker Kafka, `storage` puede ser dueño de MinIO, `queue` con backend RabbitMQ
es dueño de su propio broker. Ninguno es un recurso, y durante un tiempo `jfast
workspace compose` emitía el mismo archivo con el plugin habilitado o sin él.

Ahora lee el `jfast.toml` de cada servicio -- la lista de plugins vive ahí, no
en el archivo del workspace -- y emite lo que esos plugins declaran:

- los contenedores de `infra()`, publicados dentro del bloque de diez puertos
  de ese servicio, con `depends_on` atado a su healthcheck si tiene uno;
- un volumen por cada disco `storage` local, montado en el contenedor del
  propio servicio. Sin él los archivos subidos caen en el filesystem del
  contenedor y el siguiente `docker build` los tira, mientras las filas que los
  referencian siguen ahí;
- **nada** para `database`, `cache`, `mongo` y `qdrant`. Esos cuatro también
  declaran `infra()`, pero el grafo de recursos ya es dueño de sus contenedores,
  y emitir ambos pondría un segundo PostgreSQL anónimo al lado del que apuntan
  todas las DSN generadas.

Dos servicios con `events` habilitado obtienen **un** broker, no dos. Un broker
anuncia su propio nombre de contenedor como la dirección a la que los clientes
se reconectan, así que una segunda copia con otro nombre anunciaría una
dirección que no llega a ella.

Un plugin que el generador no puede leer -- un extra que falta en *este*
entorno, un bloque de settings que rechaza, un módulo de `[plugins.paths]` que
no importa desde la raíz del workspace -- es un warning en stderr, no silencio
y no un crash. El silencio era el defecto original: el contenedor simplemente
no estaba en el archivo, y nada lo decía. El crash era el segundo: una ruta con
puntos se llevaba puesto el compose de todo el workspace, incluidos los
servicios que estaban bien.

```
$ jfast workspace compose
UserWarning: social: cannot inspect plugin 'social_runtime' (Cannot import
plugin module 'social_plugins.runtime': No module named 'social_plugins'), so
any container it declares is missing from the generated compose file.
wrote docker-compose.yml
```

`compose` corre en la raíz del workspace, así que ahí es donde busca el módulo
que un servicio nombra con una ruta con puntos. Un paquete de plugin guardado
dentro de un solo servicio resuelve cuando ejecutas ese servicio y no cuando
generas el archivo del workspace — para eso está el warning.

Un servicio sin `jfast.toml` (un servicio Go, un directorio que todavía no
generó nadie) se saltea, y un workspace que sólo existe en memoria no tiene de
dónde leer.

### Qué se rechaza, y por qué

```
$ jfast link billing analytics-db
'billing' already binds 'core-db' to JFAST_DB_DSN. Two resources cannot share
one variable -- pass --as to choose another.
```

Dos bases de datos que caen las dos por defecto en `JFAST_DB_DSN` es la manera
común de terminar con un servicio hablando en silencio con la equivocada.
`jfast workspace validate` atrapa el resto: un puerto reclamado dos veces, un
binding a un recurso que no existe, un recurso que no usa nadie.

### Credenciales

Una contraseña por recurso, generada en el `.env` del workspace por `jfast
workspace env` y nunca sobrescrita una vez fijada -- rotar una contraseña es
una decisión, y cambiar una en silencio deja a un contenedor corriendo afuera
de su propio volumen. `jfast workspace init` agrega `.env` al `.gitignore` al
mismo tiempo, así que no se puede commitear por accidente.

El `.env` generado de cada servicio referencia el secreto en vez de repetirlo:

```
JFAST_DB_DSN=postgresql+asyncpg://app:${CORE_DB_PASSWORD}@core-db:5432/core
```

### Si vienes de un workspace 0.1

Nada se rompe. Un servicio con la lista vieja `datastores` sigue generando los
mismos contenedores en los mismos puertos; los recursos quedan implícitos en
vez de nombrados. Cuando quieras la forma explícita:

```bash
jfast workspace migrate-resources
```

Los puertos se preservan, así que el archivo de compose que produce después es
el mismo que producía antes. Correrlo dos veces no cambia nada.


## Arranca uno

```bash
jfast workspace init cometax
```

De ahí en adelante, cada servicio creado en ese directorio o por debajo se
registra solo, toma el siguiente bloque de puertos libre, y aparece en `jfast
workspace list`.

```bash
jfast new service billing --with database,cache
jfast new service catalog --with qdrant,rag
jfast new service admin --kind spa --frontend vue
jfast workspace list
```

```
workspace : cometax
api url   : http://localhost:8030

billing          api      :8010    billing
catalog          api      :8020    catalog
gateway          gateway  :8030    gateway
admin            spa      :8040    admin  (vue)
```

## Bloques de puertos, no puertos

Cada servicio es dueño de diez puertos consecutivos. No es prolijidad: los
plugins *dentro* de un servicio reclaman offsets en ese bloque — PostgreSQL en
+1, Redis en +3, Qdrant en +7 y +8. Darle al siguiente servicio un puerto
consecutivo haría que dos servicios peleen por el mismo puerto del contenedor
de base de datos.

`--port` pisa la asignación; tomar uno que ya está en uso se rechaza, con el
siguiente bloque libre en el mensaje.

---

## El gateway aparece solo

```bash
jfast new service catalog --with qdrant,rag
```

```
The workspace now has 2 backends and no gateway.
Generating one so clients need a single hostname:
  created  gateway/jfast.toml
  ...
Gateway on port 8030, routing 2 backend(s):
  /billing         -> http://billing:8010
  /catalog         -> http://catalog:8020
```

**Un solo backend no recibe gateway.** Agregaría un salto de red y una
superficie de caída a cambio de nada. Con dos es donde los clientes empiezan a
tener que conocer demasiados hostnames, así que ese es el umbral.

Los frontends no cuentan como backends: una API y una app Vue siguen siendo una
sola cosa a la que llamar.

### Refréscalo después de agregar un servicio

```bash
jfast workspace gateway --force
```

Las rutas en `gateway/jfast.toml` se derivan del archivo del workspace. No las
edites a mano — regenera, igual que regeneras compose.

### Qué hace el gateway por cada request

- Reenvía método, query, headers y body al upstream que coincide.
- Quita los headers hop-by-hop (`connection`, `transfer-encoding`, `host`…) —
  describen una sola conexión y no deben cruzar a la siguiente.
- Propaga `X-Request-ID`, así un trace sobrevive al salto y los logs del
  upstream correlacionan con el request del cliente. Setea `X-Forwarded-Host` /
  `-Proto`.
- Mapea un upstream inalcanzable a `502` y uno lento a `504`, los dos como
  `application/problem+json`.

### Qué no hace, a propósito

**Sin ruta catch-all.** Solo se proxean los prefijos configurados, así que
`/health`, `/ready` y `/metrics` siguen siendo del gateway mismo. Un catch-all
proxearía el propio liveness probe del gateway al servicio que quedara primero
en el orden.

**Sin sondeo de upstreams en `/ready`.** Un gateway cuyo upstream está
reiniciando sigue haciendo su trabajo. Sondear upstreams ahí convierte el
reinicio de un servicio en una falla de readiness de todo el sistema.

**Sin lógica de negocio.** Auth, rate limiting y caching son plugins que
habilitas en el gateway. El código de dominio no — ponlo ahí y cada equipo pasa
a shippear por la cola de deploy de tu gateway.

---

## A qué llama el frontend

```bash
jfast workspace env
```

Reescribe el `.env` de cada frontend:

```
VITE_API_URL=http://localhost:8030
```

El valor es el gateway cuando hay uno, y el único backend cuando no lo hay. Es
exactamente el string que queda viejo si se mantiene a mano, el día que aparece
un gateway, y por eso se genera.

---

## El archivo

```toml
[workspace]
name = "cometax"
base_port = 8000

[[workspace.services]]
name = "billing"
kind = "api"
port = 8010
path = "billing"

[[workspace.services]]
name = "admin"
kind = "spa"
port = 8040
path = "admin"
frontend = "vue"
```

Hazle commit. No guarda secretos, y es lo que hace que la asignación de puertos
y la generación del gateway sean reproducibles en la máquina de otra persona.

## Comandos

| Comando | Qué hace |
| --- | --- |
| `jfast workspace init <name>` | Crea el archivo |
| `jfast workspace list [--json]` | Servicios, kinds, puertos, URL de la API |
| `jfast workspace gateway [--force]` | Genera o refresca el gateway |
| `jfast workspace env` | Reescribe el `.env` de cada frontend |

`--json` en `list` devuelve el workspace entero, incluyendo `needs_gateway` y
`api_base_url` — la forma que un agente debería leer en vez de parsear el TOML.

---

## Desplegar el workspace

Un archivo, un `docker compose up`:

```bash
jfast workspace compose     # docker-compose.yml: every service, one network
jfast workspace caddy       # Caddyfile: one hostname in front of all of them
jfast workspace env         # every service's .env, DSNs included
```

Un servicio todavía puede generar el suyo, a partir de su propio grafo de
plugins:

```bash
cd billing && jfast deploy compose -o docker-compose.yml
```

Los dos no nombran los datastores igual, a propósito — lee
[deploy.md, *Dos generadores*](deploy.md#dos-generadores) antes de mover un
servicio de uno al otro, porque la variable de contraseña y el volumen cambian
junto con el nombre.

Ninguno fija un `container_name`, así que dos workspaces — o una copia vieja de
uno al lado de una nueva — corren al mismo tiempo bajo distintos nombres de
proyecto de compose (`docker compose -p old`, `docker compose -p new`).
