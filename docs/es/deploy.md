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
| +2 | PgBouncer | *(planeado)* |
| +3 | Redis | `cache` |
| +4 | MongoDB | `mongo` |
| +5 | Prometheus | `metrics` |
| +6 | Grafana | `metrics` |
| +7 | Qdrant HTTP | `qdrant` |
| +8 | Qdrant gRPC | `qdrant` |

Un servicio en 8010 obtiene PostgreSQL en 8011, Redis en 8013 y Qdrant en 8017.
Un offset de 10 o más se rechaza — chocaría con el bloque del servicio
siguiente. Un contenedor que necesita más de un puerto declara `extra_ports`.

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

## Checklist antes de producción

- [ ] `JFAST_ENV=prod` — esto por sí solo deshabilita `/info`
- [ ] `JFAST_DEBUG=false` — si no, los mensajes de excepción llegan a los
      clientes
- [ ] Todos los secretos desde el entorno, ninguno desde `jfast.toml`
- [ ] `/ready` conectado al readiness probe del orquestador, `/health` al
      liveness — no al revés
- [ ] RAG `auto_migrate` apagado; esquema manejado por Alembic
- [ ] Imagen escaneada; el contenedor corre como no-root (el generado lo hace)
