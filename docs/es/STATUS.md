# Madurez por subsistema

Un solo número de versión no puede describir este repositorio con honestidad.
El kernel está testeado, tipado y ejercitado por CI en cada push; el cliente de
Kafka nunca habló con un broker. Llamar "0.7.0" a los dos no le decía nada al
lector, y por eso el paquete vuelve a empezar en `0.1.0a1` y por eso la madurez
se registra aquí, por subsistema.

## Niveles

| Nivel | Significa |
| --- | --- |
| `beta` | Ejercitado por CI contra la dependencia real. La API todavía puede cambiar, el comportamiento se conoce. |
| `alpha` | Testeado en aislamiento, usado por el autor, no probado contra tráfico de producción. |
| `experimental` | Publicado para recibir feedback. Espera que la API se mueva. |
| `unverified` | Escrito contra APIs documentadas. **Nunca ejecutado contra la dependencia real.** |
| `broken` | Un defecto conocido, nombrado abajo. Todavía no construyas sobre esto. |

**Criterio de promoción:** un subsistema no sube de nivel porque se sienta más
terminado. Sube cuando un job de CI lo ejercita contra la dependencia real —
un contenedor, un broker, un cluster. Es la misma regla que dejó salir el
scaffold de Go y dejó afuera el de Angular.

## Kernel

| Subsistema | Nivel | Notas |
| --- | --- | --- |
| `create_app`, registro de plugins, `AppContext` | `beta` | Suite de tests completa, `mypy --strict`, descubrimiento por entry-point y detección de ciclos cubiertos. |
| Settings y carga de `jfast.toml` | `beta` | Tipado, sobreescribible por entorno. CORS, trusted hosts, límite de tamaño de body y timeout de request son settings; todos apagados salvo que se configuren. |
| Modelo de errores RFC 7807 | `beta` | Handlers para errores de dominio, HTTP, validación y no manejados. |
| `/health`, `/ready`, `/info` | `beta` | El readiness corre cada check en paralelo bajo un timeout, y reporta `timeout` aparte de `fail`. `/docs` y `/openapi.json` se cierran en producción junto con `/info`. |
| Fixtures de test | `beta` | `build_test_app`, `client_for`, `NullPlugin`. |

## Plugins

| Plugin | Nivel | Notas |
| --- | --- | --- |
| `observability` | `beta` | Logs JSON, correlación por request-id y por tenant. Cero dependencias. |
| `metrics` | `beta` | Labels por plantilla de ruta, así los parámetros de path no pueden explotar la cardinalidad. |
| `contracts` (checker) | `beta` | Corre en CI contra un servicio generado; una violación rompe el build. Capas, llamadas e imports prohibidos, estructura requerida, y bloqueo del event loop. |
| `database` | `alpha` | Cableado de engine y sesión, paginación ordenada, y un filtro de tenant que lanza en vez de fallar abierto. El comportamiento del repositorio está cubierto contra SQLite; no se corre contra PostgreSQL en CI. |
| `cache` | `alpha` | Fachada de Redis y health check. No se corre contra un Redis real en CI. |
| `auth` | `alpha` | Verificación, rotación de JWKS, detección de reuso de refresh y los defaults de ataque rechazado están testeados. Sin PKCE, sin mTLS, sin proveedor de identidad real en CI. |
| `tenancy` | `alpha` | El orden de resolución es correcto y está testeado. Esto es una **convención**, no aislamiento: un `session.execute` crudo lo evita. Row-level security no está implementado. |
| `storage` (disco local) | `alpha` | Validación de claves, chequeo de symlinks, escrituras atómicas, URLs firmadas — todo testeado. |
| `storage` (S3 / MinIO) | `unverified` | Nunca se corrió contra un endpoint real de S3 o MinIO. |
| `web` (Jinja + HTMX) | `alpha` | Renderizado y con smoke test; sin test a nivel de browser. |
| `gateway` | `alpha` | Ruteo por prefijo, stripping de headers, errores problem+json. Un upstream por prefijo: sin pool, sin balanceo de carga, sin rate limiting. |
| `queue` (PostgreSQL) | `alpha` | `FOR UPDATE SKIP LOCKED`, reclamo correcto de jobs de un worker muerto. No se corre contra un PostgreSQL real en CI. |
| `queue` (Redis) | `alpha` | Visibility timeout implementado: los workers mandan heartbeat a un registro con tiempo de servidor y cualquier worker reclama los jobs en vuelo de un par muerto. Testeado contra un doble en memoria de los comandos de Redis, todavía no contra un servidor real. |
| `queue` (RabbitMQ) | `unverified` | Nunca se corrió contra un broker. |
| `events` (Kafka) | `unverified` | Nunca se corrió contra un broker. Sin outbox, así que una fila commiteada puede perder su evento. |
| `mongo` | `alpha` | Cliente y handle nada más. Sin contrato de documento, sin migraciones, sin paridad de repositorio con SQL. |
| `qdrant` | `alpha` | Cliente, health check, contenedor. |
| `rag` | `experimental` | Chunking de ancho fijo, sin reranking, sin búsqueda híbrida, y `ensure_schema` corre DDL en el arranque en vez de pasar por Alembic. |
| `notifications` (FCM) | `unverified` | La construcción del payload está testeada; nunca se hizo una entrega desde CI. |
| `channels` | `alpha` | Pub/sub declarado. El backend de memoria está cubierto por tests; los backends de redis y kafka no se corren contra un servidor real en CI. |
| `mail` | `alpha` | Plantillas, encolado y el backend de consola están testeados. Nunca se envió un mensaje a través de un servidor SMTP real desde CI. |
| `sentry` | `alpha` | Apagado por defecto. |

## Generador y deployment

| Área | Nivel | Notas |
| --- | --- | --- |
| `jfast new module` / `new service` (Python) | `beta` | Los dos layouts, el overlay de HTMX, y el cableado de Alembic y pytest se renderizan y corren en CI. |
| Scaffolds de Vue y React | `alpha` | `npm install` más `vite build` corren en CI, que es lo que detectó el bug del marker del router. Sin test en runtime. |
| Scaffold de servicio en Go | `alpha` | `go vet`, `go test`, `go build`, y después se arranca el binario y se le hace curl. |
| gRPC | `experimental` | El contrato `.proto` se genera y el puerto queda reservado. Sin stubs, sin cableado de servidor. |
| Workspaces y asignación de puertos | `beta` | Los recursos son instancias con nombre y los servicios se enlazan a ellas bajo una variable; todo el flujo se ejercita en CI. Un archivo 0.1 todavía carga y `migrate-resources` lo convierte. |
| `deploy compose` / `dockerfile` | `beta` | Derivados del grafo de plugins. Un contenedor por recurso con los connection strings al lado, y la imagen generada se buildea y se corre contra un PostgreSQL real en CI. |
| `workspace k8s` | `unverified` | Los manifiestos se afirman estructuralmente en los tests. Nunca se aplicaron a un cluster, ni siquiera a kind. |
| `deploy function` (Lambda / Cloud Run) | `unverified` | Escribe scripts en vez de correrlos; nunca se aplicó contra una cuenta real. |
| `load_secrets` (AWS / GCP) | `unverified` | Nunca se corrió contra un secret manager real. |

## Directamente ausente

Nombrado aquí para que nadie tenga que hacer grep para enterarse:

- **Sin cliente HTTP servicio-a-servicio.** Sin reintentos, sin circuit breaker, sin política de timeouts. Cada servicio generado escribe el suyo.
- **Sin tracing distribuido.** Solo logs y métricas; `request_id` te da grep, no spans.
- **Sin diagramas de esquema ni de módulos.** `jfast workspace graph` dibuja los servicios y los recursos; el esquema de base de datos y el grafo de imports de módulos, no.
- **Sin casos de uso declarados.** Lo que hace un servicio no está escrito en ningún lugar que un build pueda chequear.
- **Sin rate limiting**, ni en el gateway ni en ninguna otra parte.
- **Sin websockets ni SSE.**
- **Sin scheduler.** Los jobs diferidos existen; los recurrentes no.
- **Sin row-level security.** Ver `tenancy` arriba.
- **Sin lock de dependencias y sin cotas superiores.** Un release de FastAPI puede romper este repositorio sin aviso, y no hay registro de contra qué versiones se testeó cada commit.
- **Publicado, pero pre-alpha.** `pip install --pre jfastframework` resuelve;
  sin `--pre` no, que es la herramienta de packaging declarando la madurez de
  esta página en vez de un README pidiéndote que le creas.

[PLAN-NEXT.md](PLAN-NEXT.md) es el plan ordenado para cerrar todo eso.
