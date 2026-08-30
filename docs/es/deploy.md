# Deploy

Los artefactos de deploy se **generan a partir del grafo de plugins**, no se
mantienen a mano. Deshabilitas un plugin y su contenedor desaparece del
siguiente compose generado. Esa es toda la idea: la infraestructura no puede
desviarse de lo que la aplicación realmente carga.

## Bloques de puertos

Un servicio es dueño de diez puertos consecutivos a partir de su puerto base
(`[app].port`). Cada plugin declara un offset dentro del bloque.

| Offset | Convención | Lo declara |
| --- | --- | --- |
| +0 | HTTP API | el servicio |
| +1 | PostgreSQL | `database` |
| +2 | Kafka | `events` |
| +3 | Redis | `cache` |
| +4 | MongoDB | `mongo` |
| +5 | Prometheus | `metrics` |
| +6 | Grafana / RabbitMQ / MinIO | `metrics`, `queue`, `storage` |
| +7 | Qdrant HTTP | `qdrant` |
| +8 | Qdrant gRPC | `qdrant` |
| +9 | gRPC | el servicio |

Un servicio en 8010 obtiene PostgreSQL en 8011, Redis en 8013 y Qdrant en 8017.
Un offset de 10 o más se rechaza — chocaría con el bloque del servicio
siguiente. Un contenedor que necesita más de un puerto declara `extra_ports`.

Tres plugins comparten el +6 porque los tres son opt-in y no se espera que un
servicio corra los tres. Si el tuyo lo hace, movés uno: cada offset de arriba
es un setting (`[plugin.queue] rabbitmq_port_offset`, y así).

## Generar el compose

```bash
jfast deploy compose --stdout          # inspect
jfast deploy compose -o docker-compose.yml
jfast deploy compose --base-port 8020  # override the block base
```

Dado:

```toml
[app]
name = "billing"
port = 8010

[plugins]
enabled = ["observability", "metrics", "database", "cache"]
```

obtienes un servicio `api` más `postgres` (8011) y `redis` (8013), con
volúmenes nombrados, healthchecks y condiciones `depends_on` conectadas a esos
healthchecks para que la API no arranque contra una base de datos que todavía
no acepta conexiones.

Prometheus y Grafana son opt-in incluso con `metrics` activado — la mayoría de
los servicios hacen scrape desde un Prometheus central en vez de correr el
suyo:

```toml
[plugin.metrics]
include_infra = true
```

### Memoria compartida en PostgreSQL

El contenedor de PostgreSQL generado lleva `shm_size: 1gb`. El default de
Docker es 64 MB, y `/dev/shm` es donde una query paralela guarda su memoria de
trabajo — así que pasado el tamaño de tabla en el que el planner empieza a
paralelizar, esa query falla con `could not resize shared memory segment`. Un
500 en exactamente las queries que importan y en ninguna otra, que es por qué
parece aleatorio hasta que alguien lo correlaciona con la cantidad de filas.

Cualquier plugin puede pedir lo mismo: `InfraService(..., shm_size="1gb")`.

## Generar un Dockerfile

```bash
jfast deploy dockerfile
```

Produce una imagen liviana que corre como usuario no-root (uid 10001), instala
las dependencias en una capa cacheada antes de copiar el código, y trae un
`HEALTHCHECK` que pega a `/health`.

Correr contenedores como root es un hallazgo en toda revisión de seguridad, y
arreglarlo después implica reconstruir capas de imagen en toda la flota. El
Dockerfile generado arranca correcto.

### Workers

Un worker de uvicorn es un proceso de Python en un core. Un request pasa la
mayor parte de su vida fuera de la base de datos — serializando, validando,
renderizando — así que un servicio puede estar lejísimos del límite de su base
y estar igual saturado: medido sobre una página de feed, 6.5 ms en PostgreSQL
contra 79 ms punta a punta con concurrencia 16.

Por eso el entrypoint deriva la cantidad de workers cuando arranca el
contenedor:

```
JFAST_WORKERS=4 docker run …    # explicit wins
                                # otherwise: the container's CPU quota, capped at 8
```

Se deriva al arrancar y no se hornea en la imagen porque la imagen no sabe
cuánto CPU le van a dar. `nproc` solo es la respuesta equivocada: reporta los
cores del *host*, así que un contenedor limitado a medio core arrancaría tantos
workers como cores tenga la máquina. cgroup v2 publica la cuota real
(`/sys/fs/cgroup/cpu.max`), así que eso se lee primero y `nproc` queda de
fallback. El tope está porque pasado cierto punto los workers sólo compiten por
el mismo core, y cada uno cuesta una copia entera de la memoria de la app.

Para fijar un número en la imagen, `render_dockerfile(workers=4)`.

## Secretos

El compose generado referencia variables de entorno en vez de embeber los
valores:

```yaml
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}
```

La forma `:?` hace que compose falle con un mensaje claro en vez de arrancar
PostgreSQL en silencio con una contraseña vacía.

Nunca pongas un DSN, una key o una contraseña en `jfast.toml` — se commitea.
Los settings que guardan secretos son de tipo `SecretStr`, lo que además los
mantiene fuera de `jfast describe` y de `/info`.

## El archivo generado es generado

`docker-compose.generated.yml` lleva un encabezado que lo dice. Las ediciones a
mano se pierden en la siguiente corrida. Si necesitas algo que el generador no
emite:

- infraestructura que un plugin posee → agrégala al `infra()` de ese plugin
- algo específico de un solo entorno → un archivo de override de compose
  (`docker-compose.override.yml`), que compose mergea automáticamente

## Kubernetes

No implementado. La fase 4 en [PLAN.md](../PLAN.md) cubre la generación de
Deployment, Service, ConfigMap, Secret y HPA a partir de las mismas
declaraciones `infra()`. Hasta entonces, escribe los manifiestos a mano — y no
asumas que la convención de bloques de puertos mapea limpio sobre la red de un
cluster, donde los servicios se direccionan por nombre DNS y las colisiones de
puertos no son un problema.

## Protecciones de borde

Cuatro cosas vienen encendidas antes de que configures nada, porque `jfast
deploy function` pone un servicio en Lambda sin nada adelante, y `uvicorn
main:app` en una laptop tampoco tiene nada.

| Setting | Default | Se apaga con |
| --- | --- | --- |
| `max_body_bytes` | `2097152` (2 MiB) | `0` |
| `request_timeout` | `30.0` | `0` |
| `security_headers` | `true` | `false` |
| `trusted_proxies` | loopback + rangos privados | `[]` |

TOML no tiene null, así que `0` es la forma en que un archivo de config dice
"sin límite". 2 MiB está muy por encima de cualquier body JSON para el que el
framework genere un handler, y muy por debajo de lo que cuesta bufferear uno;
30s es el `[plugin.gateway] timeout` del propio gateway generado, así que el
servicio se rinde en el mismo momento que lo hace lo que tiene adelante, en
vez de retener un worker por una respuesta que ya nadie espera.

`jfast new service --with storage` escribe números más grandes en `jfast.toml`
(25 MiB, 120s) — un servicio de uploads necesita ambos, y una conexión móvil
lenta enviando 25 MiB no termina el body en 30 segundos.

### Proxies de confianza

`X-Forwarded-For` lo escribe quien haya mandado el request, así que solo se le
cree cuando el peer está en `trusted_proxies`. La cadena se recorre entonces
desde la **derecha**, y se detiene en el primer hop que no es uno de tus
proxies — todo lo que está más a la izquierda lo agregó alguien sin derecho a
tu confianza, incluido el cliente.

La lista por defecto es loopback más los rangos privados (`10/8`,
`172.16/12`, `192.168/16`, `fc00::/7`), que es donde está el borde en todo lo
que este framework genera: Caddy en un bridge de compose, un ingress en una
red de pods, un sidecar en localhost. Un request que llega desde una dirección
pública es un cliente hablándote directo, y su `X-Forwarded-For` es una
sugerencia.

Recórtala al rango real del balanceador en cualquier despliegue donde un
cliente pueda alcanzar el servicio desde dentro de la red privada:

```toml
[app]
trusted_proxies = ["10.0.4.0/24"]     # solo la subred del ingress
```

`["*"]` confía en todos los peers. Es la única respuesta viable en una
plataforma cuyo front end no tiene dirección estable, y es equivocada en
cualquier lugar donde el servicio también sea alcanzable directamente.

La dirección resuelta reemplaza `scope["client"]`, así que
`request.client.host` ya es la correcta. El código que prefiera ser explícito
lee `request.state.client_ip`, o:

```python
from jfastframework.middleware import client_ip

key = f"ip:{client_ip(request)}"
```

`X-Forwarded-Proto` desde un peer de confianza fija `request.url.scheme` de la
misma forma, que es lo que permite que HSTS sepa si el request llegó de verdad
sobre TLS.

### Headers de seguridad

Toda respuesta lleva `X-Content-Type-Options`, `X-Frame-Options`,
`Referrer-Policy`, `Permissions-Policy` y una Content-Security-Policy. Un
header que la aplicación puso ella misma nunca se sobrescribe, así que una
ruta que necesita una política más laxa pone la suya y el resto del servicio
sigue estricto.

`X-Frame-Options: DENY` sale **junto con** `frame-ancestors 'none'` de la CSP,
no en su lugar. La directiva de CSP lo reemplaza en un navegador actual, pero
se ignora en una política report-only y en los WebViews embebidos y frames en
modo IE que siguen siendo la razón por la que el clickjacking se sigue
reportando.

La política por defecto es **enforced**, y está escrita alrededor de lo que
carga la salida del propio framework:

```
default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com;
style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:;
connect-src 'self'; frame-ancestors 'none'; base-uri 'self';
form-action 'self'; object-src 'none'
```

`'unsafe-inline'` está ahí porque lo exige la salida que no puede romper: los
dos templates base de HTMX traen un handler `htmx:responseError` inline y el
scaffolder deja en paz una copia existente, y la página `/docs` de FastAPI es
una llamada `SwaggerUIBundle` inline. Una política que tira un 500 en la
primera página de un servicio nuevo se apaga dentro de la hora. Lo que sí
compra el default es todo lo que no cuesta nada: nada de framing, nada de
`<base>` inyectado, nada de form post fuera de origen, nada de plugin
embebido, y nada de `fetch` ni `<img>` fuera de origen por donde exfiltrar.

Fuera de producción la política también permite `https://cdn.jsdelivr.net`,
`https://fonts.googleapis.com`, `https://fonts.gstatic.com` y
`https://fastapi.tiangolo.com` — los CDNs desde los que cargan `/docs` y
`/redoc`. Salen solos cuando se cierra el esquema OpenAPI, que es lo que hace
`JFAST_ENV=prod` por defecto. La política más estricta llega con el entorno,
no con una edición que alguien tenga que recordar.

Para ir más lejos, en este orden:

1. `[plugin.web] htmx_cdn = false` y guarda `htmx.min.js` en `static/`. Eso
   saca `https://unpkg.com` de lo que la política tiene que permitir.
2. Pon `csp_report_only = true` con tu propia `csp` más estricta y un
   `report-uri`, y observa qué se rompe durante un release.
3. Pon esa política en `csp` sin `'unsafe-inline'` y apaga report-only.
   Cualquier `<script>` inline que quede en tus templates tiene que pasar a
   ser un archivo primero.

### HSTS

Apagado en local y dev, un año en producción, y omitido en cualquier request
que no haya llegado por HTTPS — un navegador ignora HSTS sobre texto plano de
todos modos (RFC 6797), y quien lea `curl -I http://localhost` no debería ver
al servicio reclamar una garantía que nada está aplicando.

La condición es real porque el error no se puede deshacer desde la aplicación:
el navegador recuerda HSTS durante todo el `max-age`, así que encenderlo por
accidente envenena `http://localhost` por un año y ninguna limpieza del caché
de la app lo arregla.

```toml
[app]
hsts_seconds = 600            # pídelo donde sea, corto, para probar
hsts_preload = false          # entrar a la preload list es ~irreversible
```

## Checklist antes de producción

- [ ] `JFAST_ENV=prod` — esto por sí solo deshabilita `/info`, cierra `/docs`
      y `/openapi.json`, aprieta la CSP y enciende HSTS
- [ ] `JFAST_DEBUG=false` — si no, los mensajes de excepción llegan a los
      clientes
- [ ] Todos los secretos desde el entorno, ninguno desde `jfast.toml`
- [ ] `/ready` conectado al readiness probe del orquestador, `/health` al
      liveness — no al revés
- [ ] RAG `auto_migrate` apagado; esquema manejado por Alembic
- [ ] Imagen escaneada; el contenedor corre como no-root (el generado lo hace)
- [ ] `trusted_proxies` recortado al rango real del balanceador, si un cliente
      puede alcanzar el servicio desde dentro de la red privada
- [ ] `max_body_bytes` y `request_timeout` dimensionados para lo que este
      servicio realmente acepta, no dejados en la suposición del framework
