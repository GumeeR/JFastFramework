# JFastFramework

**Un framework FastAPI basado en plugins para microservicios, hecho para ser manejado por agentes de IA.**

Estado: `0.1.0a1` — pre-alpha, en PyPI. La versión se reinició desde `0.7.0` a
propósito; [CHANGELOG.md](CHANGELOG.md#renumbering) dice por qué. La madurez es
por subsistema y no global: [STATUS.md](STATUS.md) lista qué es confiable, qué
está sin verificar y qué se sabe que está roto. [PLAN-NEXT.md](PLAN-NEXT.md) es
el camino a 1.0; [PLAN.md](PLAN.md) registra lo que falta.

---

**[jfastframework en PyPI](https://pypi.org/project/jfastframework/)** &middot; **[Documentación](https://jfabrizzio5.github.io/JFastFramework/latest/)**

---

## Un comando

```bash
pip install --pre jfastframework   # --pre: 0.1.0a1 is a pre-release
jfast start shop
```

Un monolito modular en Python con PostgreSQL + pgvector, Redis y trabajos en
segundo plano; un frontend Vue 3; un Caddyfile; un archivo de compose. Todo
conectado entre sí — la URL de la API del frontend, el DSN de las migraciones,
los puertos de los contenedores — en vez de cuatro carpetas que casualmente
quedaron una al lado de la otra.

Un monolito y no tres servicios, a propósito: todavía no sabes dónde están las
costuras. Separar después es una mudanza; volver a unir es una reescritura.
Cuando un módulo le queda chico, `jfast new service` lo promueve.

¿Prefieres elegir? `jfast init` pregunta. ¿Prefieres flags? Cada elección es uno.

---

## Qué problema resuelve

Los generadores de scaffolding copian código. Cada servicio generado se vuelve
un fork congelado en el momento de generarlo: arreglas un bug en el cliente HTTP
compartido y lo parchas a mano en doce lugares.

JFast separa las dos preocupaciones que los generadores mezclan:

| | |
| --- | --- |
| **Runtime** (`jfastframework`) | Una librería versionada que tus servicios **importan**. Lo arreglas una vez, subes el pin. |
| **Generador** (CLI `jfast`) | Emite solo el código que es genuinamente tuyo. |

Todo lo que está por encima del kernel es un plugin.

```toml
[plugins]
enabled = ["observability", "metrics", "database", "cache", "queue"]
disabled = ["sentry"]
```

Borra `"cache"` y el cliente Redis, su health check y su contenedor en el
archivo de compose generado desaparecen juntos. La infraestructura se deriva del
grafo de plugins, así que no puede desviarse de lo que la app realmente carga.

## Plugins

| Plugin | Qué hace | Extra |
| --- | --- | --- |
| `observability` | Logs JSON, correlación por request-id y tenant | — |
| `metrics` | Métricas RED de Prometheus, `/metrics` | `metrics` |
| `database` | SQLAlchemy async, sesiones, cableado de Alembic | `db` |
| `cache` | Caché Redis, pub/sub | `cache` |
| `queue` | Trabajos en segundo plano sobre PostgreSQL, Redis o RabbitMQ | `queue` |
| `events` | Publicación/suscripción con Kafka | `kafka` |
| `mongo` | MongoDB vía Motor | `mongo` |
| `qdrant` | Base de datos vectorial Qdrant | `qdrant` |
| `rag` | Recuperación sobre pgvector o Qdrant | `rag` |
| `web` | Renderizado de parciales con Jinja2 + HTMX | `web` |
| `gateway` | Reverse proxy por prefijo | `gateway` |
| `auth` | Verificación de JWT, scopes, revocación, login social | `auth` |
| `storage` | Archivos en discos locales, S3 o MinIO | `storage` |
| `tenancy` | Tenant desde un claim del token, subdominio o path | — |
| `notifications` | Push por Firebase Cloud Messaging | `fcm` |
| `sentry` | Reporte de errores y de performance | `sentry` |

Los plugins de terceros se registran por el mismo entry-point group, así que
nada de lo que hay aquí es privilegiado.

---

## No solo Python

Un servicio JFast no es «un servicio escrito con JFast» — es un servicio que
cumple [el contrato](docs/service-contract.md): `/health`, `/ready`,
`X-Request-ID`, `problem+json`, configuración `JFAST_*`, un bloque de diez
puertos, logs JSON en stdout.

```bash
jfast new service edge --language go --grpc
```

Ese servicio Go tiene **cero dependencias de terceros** e implementa el contrato
en ~300 líneas vendoreadas. El gateway rutea hacia él sin saber que es Go; el
workspace le asigna sus puertos; Caddy lo expone junto a todo lo demás — porque
todos le hablan al contrato, no al lenguaje.

CI corre `go vet`, `go test`, `go build`, arranca el binario y le hace curl. Un
scaffold que nadie corrió es un pasivo que parece una feature.

`--grpc` genera el contrato `.proto`. **No** genera stubs ni cablea un servidor
— mira [proto/README.md](src/jfastframework/templates/proto/proto/README.md.j2)
para saber por qué, y para los comandos con los que hacerlo tú mismo.

---

## Colas y eventos

Dos cosas distintas, dos plugins:

```toml
[plugin.queue]
backend = "postgres"    # or "redis", "rabbitmq"
```

```python
@tasks.task("send_invoice_email")
async def send_invoice_email(payload: dict) -> None: ...

await queue.enqueue(Job(task="send_invoice_email", payload={"id": 7}))
```

Empieza con PostgreSQL: encolar comparte la transacción que produjo el trabajo,
así que un rollback se lleva el job con él. Redis compra latencia, RabbitMQ
compra ruteo. [Cuál elegir, y por qué](docs/queues-and-events.md).

La entrega es at-least-once — los handlers tienen que ser idempotentes. Los
reintentos están acotados y el backoff tiene tope; los jobs agotados van a
dead-letter en vez de quedar en loop.

Los eventos son la otra mitad: `events` es Kafka, para «esto pasó» y no «haz
esto».

---

## Frontends

**Renderizado en el servidor**, sin build step:

```bash
jfast new service storefront --kind web
jfast new module product --ui htmx
```

**SPA**, Vue 3 o React con Vite y Tailwind v4:

```bash
jfast new service admin --kind spa --frontend vue
cd admin && jfast new view Facturas
```

`jfast new view` crea `src/ModuloFacturas/{Components,Pages,Routes,Services}` y
lo registra en el router y en el sidebar, en sus comentarios marcadores — de
forma idempotente, y fallando ruidosamente si falta un marcador.

Los dos frontends se instalan y se buildean en CI. Ese job existe por un bug
real: el comentario marcador quedó dentro de un comentario de bloque, cuyo `*/`
interno lo cerró antes de tiempo y dejó el router sintácticamente inválido.
Todos los greps pasaron. Solo `vite build` lo atrapó.

Angular y React Native **no** se generan. [Por qué](docs/frontend.md#angular).

---

## Workspaces, gateway, Caddy

```bash
jfast workspace init cometax
jfast new service billing --with database,cache
jfast new service catalog --with qdrant,rag     # a gateway appears here
jfast workspace compose && jfast workspace caddy
```

Los servicios se registran solos y toman el siguiente bloque libre de diez
puertos. En el **segundo** backend se genera un gateway automáticamente — con un
solo backend a propósito no aparece, porque agregaría un salto y una superficie
de caída a cambio de nada.

Caddy es el borde (TLS, HTTP/3, compresión, la SPA ya buildeada); el gateway es
el proxy de aplicación detrás. Los backends viven bajo `/api` en cualquier caso,
así que el build de producción del frontend sobrevive a que aparezca un gateway.

[docs/workspaces.md](docs/workspaces.md) · [docs/deploy.md](docs/deploy.md)

---

## Migraciones y tests, ya cableados

Todo servicio generado trae `alembic.ini`, `migrations/env.py`, `pytest.ini` y
`conftest.py`. `env.py` lee el mismo `JFAST_DB_DSN` que lee la app — una
migración no puede correr contra una base distinta a la del servicio — e importa
automáticamente los modelos de cada módulo, así que autogenerate nunca emite en
silencio una migración vacía. [Detalles y las trampas](docs/migrations-and-tests.md).

---

## Autenticación

```toml
[plugin.auth]
mode = "jwks"                    # jwks | public_key | secret
jwks_url = "https://id.example.com/.well-known/jwks.json"
issuer = "https://id.example.com/"
audience = "billing"
algorithms = ["RS256"]
```

```python
@router.post("/invoices")
async def create(caller: Principal = Depends(require_scopes("invoices:write"))):
    ...
```

Verificación, scopes y roles, rotación de claves JWKS, rotación de refresh con
detección de reúso, y revocación compartida entre réplicas a través de Redis.

Los defaults rechazan los ataques que no parecen fallas: `alg: none`, la
confusión RS256→HS256 (configurar las dos familias de algoritmos a la vez se
rechaza al arrancar — esa combinación *es* el ataque), tokens emitidos para un
servicio hermano, y un clock skew generoso. Las razones del rechazo van al log;
el cliente recibe un 401 pelado.

**El tenancy deja de ser falsificable.** Sin auth, `tenant_id` viene del header
`X-Tenant-ID` — lo puede poner cualquiera con curl. Con auth, viene de un claim
firmado.

No hay `/auth/login`: verificar una contraseña contra tu tabla de usuarios es
trabajo de tu aplicación. `auth.issuer` está ahí para tu propia ruta.
[docs/auth.md](docs/auth.md).

## Kubernetes

```bash
jfast workspace k8s --host app.example.com
kubectl apply -k k8s/overlays/dev
```

`jfast init` pregunta si lo necesitas. Obtienes un árbol de kustomize:
Deployment, Service, ConfigMap, HPA y PodDisruptionBudget por servicio, un
Ingress sirviendo `/api` — la misma forma pública que el Caddyfile generado — y
overlays `dev`/`prod`.

El liveness sondea `/health`, el readiness sondea `/ready`. Ese contrato de dos
endpoints es lo que evita que un parpadeo de la base reinicie todos los pods
sanos a la vez.

**Las bases de datos no se generan.** Un StatefulSet de PostgreSQL salido de un
scaffolder es la forma en que la gente pierde datos. Los manifiestos leen un DSN
desde un Secret. [docs/kubernetes.md](docs/kubernetes.md).

## Contratos: reglas de las que un agente no se puede desviar

`AGENTS.md` dice qué hacer. Un **contrato** dice qué está permitido, y algo lo
verifica — que es la diferencia entre una regla y una sugerencia.

Todo servicio generado trae un `contracts.toml` que es tuyo:

```toml
[project]
owns = "Invoices and payments."
does_not_own = "Customers. Ask the catalog service."

[layers.domain]
paths = ["modules/*/[!_]*.py"]
may_import = []
forbid_packages = ["fastapi", "sqlalchemy", "pydantic"]

[[rules.forbid_call]]
pattern = "os.getenv"
except_in = ["settings.py"]
why = "Configuration is typed. Add a field to a settings model."
```

```bash
jfast contracts check
```

```
modules/invoice/repository.py:1: layer-package: 'storage' must not import 'fastapi'
  (Data access. No business rules.)
```

El mismo archivo enciende el check para el bug que nunca lanza una excepción:
una llamada bloqueante dentro de un `async def`, que frena todos los demás
requests del worker y aparece como latencia en un lugar completamente distinto.

```
blocking_demo.py:14: async-blocking: requests.get() blocks the event loop inside async send()
```

Exit distinto de cero — en CI, un build fallido. Un agente que genera código a
toda velocidad se desvía de la prosa; no se desvía de un check que falla.

Tres audiencias, un archivo: el build lo lee con `check`, un agente con
`jfast contracts show --json`, un reviewer con el `CONTRACTS.md` generado. Las
exenciones van inline y exigen una razón. [docs/contracts.md](docs/contracts.md).

## Por qué los agentes de IA son una audiencia de primera clase

```bash
jfast contracts show --json  # the rules THIS project holds itself to
jfast describe --json        # settings schema, plugin graph, providers, infra
jfast workspace list --json  # services, ports, API base URL, needs_gateway
jfast doctor
```

Nada de grep. Además `.jfast/skills/` — una carpeta por tarea con un `SKILL.md`
que dice cuándo usarla y los pasos exactos — y [AGENTS.md](AGENTS.md), las
reglas que un agente tiene que seguir aquí.

---

## Documentación

El sitio se construye a partir de estos mismos archivos: **<https://jfabrizzio5.github.io/JFastFramework/>**

| Documento | Contenido |
| --- | --- |
| [docs/local-setup.md](docs/local-setup.md) | Instalar desde un checkout y hacer tu primer proyecto |
| [docs/contracts.md](docs/contracts.md) | Reglas por proyecto, verificadas |
| [docs/auth.md](docs/auth.md) | JWT: modos, los ataques rechazados, revocación, login con Google |
| [docs/storage.md](docs/storage.md) | Discos, URLs firmadas, S3 y MinIO |
| [docs/multitenancy.md](docs/multitenancy.md) | Subdominios, orden de confianza, qué no es |
| [docs/cloud.md](docs/cloud.md) | Gestores de secretos, funciones serverless, push |
| [docs/kubernetes.md](docs/kubernetes.md) | Manifiestos, probes, qué no se genera |
| [docs/service-contract.md](docs/service-contract.md) | Qué tiene que hacer todo servicio, en cualquier lenguaje |
| [docs/modules.md](docs/modules.md) | Layouts de módulo, HTMX, tipos de servicio |
| [docs/datastores.md](docs/datastores.md) | PostgreSQL, Redis, Mongo, Qdrant |
| [docs/queues-and-events.md](docs/queues-and-events.md) | Jobs, streams, backends |
| [docs/frontend.md](docs/frontend.md) | HTMX, Vue, React, el generador de vistas |
| [docs/workspaces.md](docs/workspaces.md) | Muchos servicios, el gateway |
| [docs/migrations-and-tests.md](docs/migrations-and-tests.md) | Alembic, pytest |
| [docs/plugins.md](docs/plugins.md) | Escribir un plugin |
| [docs/deploy.md](docs/deploy.md) | Compose, Caddy, Dockerfile |
| [docs/skills.md](docs/skills.md) | Escribir una skill |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Decisiones y sus costos |
| [PLAN.md](PLAN.md) | Hecho, parcial, sin empezar |

## Verificar

```bash
pytest                             # 397 framework tests
ruff check src tests docs-site && ruff format --check src tests docs-site
mypy src                           # strict

bash scripts/smoke.sh              # both module layouts, HTMX, alembic, a booting service
bash scripts/smoke_contracts.sh    # a generated service passes its own contract
bash scripts/smoke_auth_k8s.sh     # auth guards routes; manifests parse
bash scripts/smoke_workspace.sh    # workspace, gateway, view patching
bash scripts/smoke_start.sh        # the default stack, end to end
bash scripts/smoke_docs.sh         # the quickstart, run exactly as written
bash scripts/smoke_go.sh           # go vet, test, build, run, curl   (needs go)
bash scripts/smoke_frontend.sh     # npm install + vite build         (needs node)
python docs-site/build.py --version latest --output site/latest
python docs-site/check.py site/latest
```

## Qué no está verificado

Dicho de frente, porque un framework que exagera su cobertura es peor que uno
que admite el hueco:

- **RabbitMQ y Kafka** están escritos contra APIs documentadas, pero nunca se
  probaron de ida y vuelta contra brokers reales en CI.
- **El multi-tenancy** es una convención que hace cumplir `BaseRepository`, no
  una garantía de aislamiento. Row-level security es fase 2.
- **RAG** trocea a ancho fijo y sin reranking.
- **Angular, React Native, Laravel, .NET** no se generan en absoluto.

## Licencia

MIT.
