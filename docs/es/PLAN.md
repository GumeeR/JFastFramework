# JFastFramework — Roadmap

Las fases están ordenadas por dependencia, no por ambición. Cada una termina
con algo usable; nada se entrega a medias dentro de la fase siguiente.

Leyenda: `[x]` hecho · `[~]` parcial, con los huecos nombrados · `[ ]` sin
empezar

---

## Fase 0 — Kernel (hecho)

Lo más chico que hace posible cada fase posterior.

- [x] `create_app()` — resolución de plugins, registro, orquestación del
      lifespan
- [x] `JFastSettings` / `JFastConfig` — config tipada desde `jfast.toml` + env
- [x] `AppContext` — indirección `provide` / `require` entre plugins
- [x] Contrato de plugin — `meta`, `Settings`, hooks de ciclo de vida,
      `infra()`, `describe()`
- [x] Registry — descubrimiento por entry points, listas allow/deny, orden por
      dependencias, detección de ciclos, detección de providers duplicados
- [x] Modelo de errores RFC 7807 con handlers para excepciones de dominio, de
      HTTP, de validación y no manejadas
- [x] `/health`, `/ready`, `/info`
- [x] Fixtures de test (`build_test_app`, `client_for`, `NullPlugin`)
- [x] Suite de tests del kernel

**Criterios de salida cumplidos:** un servicio es `create_app()` y un
`jfast.toml`.

---

## Fase 1 — Plugins integrados (casi hecho)

- [x] `observability` — logging en JSON, correlación de request-id y tenant-id,
      access log. Activado por defecto, cero dependencias extra.
- [x] `metrics` — métricas RED de Prometheus, labels por plantilla de ruta (sin
      explosión de cardinalidad por los path parameters), `/metrics`.
- [x] `sentry` — apagado por defecto, DSN como `SecretStr`.
- [x] `database` — engine async de SQLAlchemy, session factory, dependencia de
      sesión con scope de request y commit/rollback.
- [x] `cache` — fachada de Redis más el cliente crudo, health check no crítico.
- [x] `mongo` — cliente Motor y handle de base de datos, para datos con forma
      de documento.
- [x] `qdrant` — cliente, health check, contenedor con puertos HTTP y gRPC.
- [x] `web` — Jinja2, archivos estáticos, render de parciales con HTMX,
      fragmentos HTML de error para los requests de HTMX.
- [x] Protocolo `VectorStore` con implementaciones en pgvector y Qdrant; el
      plugin `rag` elige una desde la config y falla al arrancar, nombrando el
      plugin que falta, cuando la elección no coincide con el grafo habilitado.
- [~] `rag` — store y embedder intercambiables, router de
      ingest/search/delete. **Huecos:** solo chunking de tamaño fijo (malo para
      código y tablas); sin reranking; sin búsqueda híbrida BM25 + vectorial;
      `ensure_schema` corre DDL al arrancar en vez de pasar por Alembic.

### Lo que falta en esta fase

- [ ] `auth` — verificación de JWT RS256, fetch de JWKS con rotación y caché,
      dependencias de scope
- [ ] `worker` — wrapper de Arq: registro de tareas, política de reintentos,
      dead-letter queue
- [ ] `internal_client` — HTTP service-to-service con reintentos, backoff
      exponencial, **circuit breaker**, propagación de `X-Request-ID`
- [ ] `websockets` — connection manager con un backplane de pub/sub en Redis

---

## Fase 2 — Multi-tenancy (sin empezar)

El pitch de toda fábrica de SaaS y la parte que todo el mundo hace mal, tarde.
Decide la estrategia antes de escribir nada de esto, porque cambiarla después
es una migración de datos, no un refactor.

- [ ] Elegir: columna `tenant_id` + filtro forzado · row-level security de
      PostgreSQL · un esquema por tenant. **Inclinado a RLS** — la única opción
      donde olvidar un filtro no es una fuga de datos.
- [ ] Resolución del tenant desde claim del JWT / header / subdominio
- [ ] Contexto de tenant propagado a la sesión de DB (`SET LOCAL app.tenant_id`)
- [ ] Rate limiting y cuotas por tenant
- [ ] Tests de que una query sin contexto de tenant devuelve cero filas

**Trade-off que hay que aceptar ahora:** `BaseRepository` filtra por tenant,
pero un `session.execute` crudo lo saltea. Hasta que llegue RLS, el aislamiento
entre tenants es una convención, no una garantía. No lo vendas como garantía.

---

## Fase 3 — El generador (parcialmente hecho)

- [x] Plantillas Jinja2 como archivos reales — con lint, diff y tests
- [x] `jfast new module <name>` — layout por capas con tests y un README
- [x] `--layout screaming` — dominio libre de framework, un archivo por caso de
      uso
- [x] `--ui htmx` — overlay compuesto sobre cualquiera de los dos layouts, no
      duplicado
- [x] `jfast new service <name> --kind api|web` — un servicio entero desde cero
- [x] Entornos de Jinja separados para que las plantillas HTML conserven sus
      `{{ }}` de runtime
- [x] Nombres de tabla pluralizados, sobreescribibles con `--table`
- [x] Sello `.jfast-template` que registra la versión del framework y el
      contexto
- [x] Layout de Alembic en el servicio generado (env.py lee el DSN de la app e
      importa solo los modelos del módulo; compare_type y
      compare_server_default prendidos)
- [x] pytest.ini y conftest.py con fixtures de app/client
- [x] `jfast init` — instalador interactivo: kind, frontend, datastores, puerto
- [x] `jfast new service --with database,cache,qdrant,rag` — la lista de
      plugins, los bloques de config, las claves de .env y los extras pineados
      salen todos de ahí
- [x] `--kind spa --frontend vue|react` — proyecto Vite + Tailwind v4
- [x] `jfast new view` — estructura Modulo<Name> con parcheo idempotente del
      router y del sidebar
- [ ] Un workflow de CI en el servicio generado
- [ ] `jfast upgrade` — reaplicar plantillas más nuevas sobre un árbol
      existente y mostrar un diff. **Esto es lo que el generador de CometaX
      nunca tuvo**, y la razón por la que los servicios generados derivan.
- [ ] `jfast new plugin <name>` — scaffold de un paquete de plugin de terceros
- [ ] Scaffold de Angular (`--frontend angular`), condicionado a un job de CI
      que corra `ng build` — una config que nadie construyó es peor que no
      tener scaffold
- [ ] Un job de Node en CI que corra `npm install` + `vite build` sobre los
      proyectos Vue y React generados. **Hasta que exista, esos scaffolds están
      sin verificar más allá del renderizado de archivos.**
- [ ] Dockerfile y entrada de compose para proyectos `--kind spa`
- [ ] Mejoras de RAG: chunking consciente de la estructura, reranking, búsqueda
      híbrida

---

## Fase 3b — Workspaces y el gateway (hecho)

- [x] `jfast.workspace.toml` — los servicios se registran solos, toman el
      siguiente bloque libre de diez puertos y anotan a qué debe llamar el
      frontend
- [x] `jfast workspace init | list | gateway | env`
- [x] Plugin `gateway` — ruteo por prefijo, limpieza de headers hop-by-hop,
      propagación de `X-Request-ID`, 502/504 como problem+json, sin catch-all,
      sin sondear upstreams en el readiness
- [x] El gateway se genera automáticamente al segundo backend, y no antes
- [ ] Un solo archivo de compose para todo el workspace, gateway al frente, red
      compartida
- [ ] Auth en el gateway (necesita el plugin `auth` de la fase 1)
- [ ] Rate limiting en el gateway

---

## Fase 4 — Deployment (parcialmente hecho)

- [x] `Plugin.infra()` — los plugins declaran sus contenedores
- [x] `jfast deploy compose` — docker-compose derivado del grafo de plugins
- [x] `jfast deploy dockerfile` — non-root, healthcheck, cacheado por capas
- [x] `extra_ports` para contenedores multi-puerto (Qdrant HTTP + gRPC)
- [ ] `jfast deploy k8s` — Deployment, Service, ConfigMap, Secret, HPA
- [ ] `jfast deploy env` — `.env.example` derivado del schema de settings de
      cada plugin, para que la doc de config no pueda quedar vieja
- [x] Registro de asignación de puertos — el esquema de bloques de 10 puertos
      de CometaX sin un IAM central como dependencia dura
      (jfast.workspace.toml)
- [ ] Plantilla de workflow de GitHub Actions

---

## Fase 5 — Superficie para agentes (parcialmente hecho)

Lo que hace que "describe una app y obtenla" sea real y no una demo.

- [x] `jfast describe --json` — schema de settings, grafo de plugins,
      providers, infra
- [x] `jfast doctor` — la config resuelve, los plugins habilitados importan
- [x] Layout `.jfast/skills/` con skills de arranque
- [x] Contrato `AGENTS.md`
- [ ] `jfast routes --json` — cada ruta montada con su schema
- [ ] `jfast skills list` — enumerar las skills para que un agente pueda elegir
      una
- [ ] Convención `DESIGN.md` cableada en las plantillas del frontend
- [x] Skill `build-frontend`
- [ ] Skills de receta: `add-auth`, `add-worker`, `add-websocket`

---

## Fase 6 — Endurecimiento (empezado)

- [x] `mypy --strict` limpio en todo `src/`
- [x] `ruff check` + `ruff format --check` limpios
- [x] CI: lint, formato, tipos, tests y dos suites de humo de scaffold en
      3.11–3.13
- [ ] 90% de cobertura de tests en el kernel
- [ ] Suite de integración contra PostgreSQL y Redis reales
- [ ] Benchmark: overhead del kernel por request vs FastAPI pelado (publicar el
      número)
- [ ] Política de versionado semántico y una ventana de deprecación
- [ ] Sitio de documentación

---

## No-objetivos explícitos

Decir que no mantiene chico el kernel.

- **No es un ORM.** SQLAlchemy ya es bueno.
- **No es un control plane.** Los servicios JFast pueden registrarse contra un
  IAM, pero el framework tiene que correr standalone. El acoplamiento a
  `ApiIam` de CometaX es exactamente lo que hizo difícil de reusar a la
  generación anterior.
- **No es un framework de frontend.** Plantillas para Vue 3, opiniones sobre
  design tokens, nada más.
- **No es multi-lenguaje.** Solo Python. La mitad PHP/Laravel de CometaX es un
  problema aparte con una solución aparte.

---

## Migrar desde CometaXMicroservices

1. Elige un servicio generado — `FrameworkTest` es la víctima natural para
   empezar.
2. Reemplaza su `core/` y `config/` copiados por imports de `jfastframework`.
3. Mueve cada `os.getenv` a un modelo de settings de plugin.
4. Convierte cada `modules/<name>/` al layout de módulo de JFast.
5. Borra los archivos copiados y pinea `jfastframework~=0.1`.
6. Recién entonces migra el segundo servicio.

No los migres todos de una. La primera migración es donde salen a la superficie
las piezas que le faltan al framework, y conviene encontrarlas con un solo
servicio en riesgo.

---

## Fase 3c — Polyglot, colas y el borde (0.4.0)

- [x] `docs/service-contract.md` — lo que todo servicio debe cumplir, en
      cualquier lenguaje. La interfaz; las plantillas son implementación.
- [x] `jfastframework/languages.py` — registro de lenguajes, detección de
      toolchain
- [x] Scaffold de servicio en Go, cero dependencias de terceros, verificado en
      CI con `go vet` + `go test` + `go build` y corriendo el binario y
      pegándole con curl
- [~] gRPC — el contrato `.proto` se genera y el puerto queda reservado.
      **Huecos:** no hay generación de stubs ni cableado del server. Ambos
      están condicionados a un job de CI que haga round-trip de una llamada
      real.
- [x] Plugin `queue`: backends PostgreSQL / Redis / RabbitMQ, registro de
      tareas, worker con backoff acotado, dead-lettering y drenado
- [x] Plugin `events`: publish/subscribe con Kafka, partition keys, commit
      después de manejar el mensaje
- [x] `jfast start` — el stack por defecto y opinado, en un comando
- [x] `jfast workspace compose` / `jfast workspace caddy`
- [x] Sitio de documentación con publicación versionada y un checker de
      links/assets
- [ ] Tests de integración contra contenedores reales de RabbitMQ y Kafka.
      **Hasta que existan, esos dos backends están sin verificar.**
- [ ] Entry point del `worker` en la CLI (`jfast worker run`)
- [ ] Patrón outbox: publicar un evento en la misma transacción que la escritura

## Fase 3d — Más lenguajes y frontends (sin empezar)

Cada ítem de aquí está condicionado a la misma regla: **un job de CI que
construya y corra lo que genera.** Esa regla es la razón de que Go saliera y
Angular no.

- [ ] Scaffold de Angular, condicionado a `ng build` en CI
- [ ] Scaffold de React Native, condicionado a un bundle de Metro en CI
- [ ] Laravel — un `LanguageSpec`, un árbol de plantillas que cumpla el
      contrato, y un job de CI que corra `php artisan test`
- [ ] .NET — la misma forma, condicionado a `dotnet build` y `dotnet test`
- [ ] Servicio Node/TypeScript, para equipos que ya están ahí

El contrato es el punto de extensión. Agregar un lenguaje es: implementar las
seis secciones de `docs/service-contract.md`, registrar un `LanguageSpec`,
agregar un árbol de plantillas, agregar el job de CI.

---

## Fase 1b — Auth (0.6.0)

- [x] Plugin `auth`: verificación por JWKS / clave pública / secreto compartido
- [x] Pinning de algoritmo, verificación de `aud` e `iss`, 30s de leeway;
      mezclar algoritmos simétricos y asimétricos se rechaza al arrancar
- [x] `require_auth` / `require_scopes` / `require_roles` / `optional_auth`
- [x] Rotación de JWKS con un refresh rate-limited y fallback a la clave
      cacheada
- [x] Emisión de tokens, rotación de refresh con detección de reuso, revocación
      de la familia
- [x] Store de revocación: Redis cuando `cache` está prendido, en memoria si no
      — y el de memoria se reporta a sí mismo como no compartido
- [x] `tenant_id` desde un claim firmado en vez del header `X-Tenant-ID`
- [x] Login social (0.7.0): presets de Google / Microsoft / GitHub, state y
      nonce verificados, audience e issuer chequeados, `@auth.on_identity` como
      la costura donde una identidad verificada se convierte en tu usuario
- [ ] PKCE, para un cliente público que habla directo con el proveedor
- [ ] Identidad mTLS / SPIFFE para llamadas service-to-service
- [ ] Aislamiento de claves por tenant
- [ ] Una regla de contrato de `auth`: "ninguna ruta sin dependencia" como check

## Fase 4b — Kubernetes (0.6.0)

- [x] `jfast workspace k8s` — base de kustomize más overlays `dev`/`prod`
- [x] `jfast init` pregunta si hace falta Kubernetes
- [x] Deployment, Service, ConfigMap, HPA, PodDisruptionBudget, Ingress
- [x] Liveness en `/health`, readiness en `/ready`, startup probe; non-root,
      filesystem raíz de solo lectura, capabilities dropeadas,
      `maxUnavailable: 0`
- [x] Plantillas de Secret con placeholders; las bases de datos a propósito no
      se generan
- [ ] Aplicar los manifiestos a un cluster kind en CI. **Hasta entonces están
      afirmados estructuralmente, no probados.**
- [ ] NetworkPolicies (default-deny más allows explícitos)
- [ ] ServiceMonitor para el Prometheus Operator
- [ ] Un Job de pre-deploy para las migraciones, ordenado de forma segura
      contra el rollout

---

## Fase 6 — Storage, tenancy y nube (0.7.0)

- [x] Plugin `storage`: discos con nombre y visibilidad, drivers local y
      S3/MinIO detrás de un solo protocolo
- [x] Validación de claves en todos los backends (traversal, rutas absolutas,
      backslashes, null bytes), más un chequeo de symlinks post-resolución en
      los discos locales
- [x] URLs temporales firmadas con HMAC que cubren la clave *y* el
      vencimiento, comparación en tiempo constante, un único 403
      indistinguible para vencidas y falsificadas
- [x] `attachment` + `nosniff` en cada descarga, para que un `.html` subido no
      pueda correr script en tu origin
- [x] MinIO en el compose generado, opt-in, en el offset de puerto `+6`
- [x] Plugin `tenancy`: claim del token, subdominio, path o header, en ese
      orden de confianza; el middleware corre en el punto más interno para que
      el claim firmado sea legible
- [x] Bloque de sitio wildcard de Caddy con TLS on-demand y el endpoint `ask`
      que lo controla
- [x] `load_secrets()` desde AWS Secrets Manager o Google Secret Manager, con
      el entorno ganándole a la copia guardada
- [x] `jfast deploy function` para AWS Lambda y Cloud Run — privado por
      defecto, y escribe scripts en vez de correrlos
- [x] Plugin `notifications`: FCM HTTP v1, con un backend de consola para
      desarrollo
- [ ] **Row-level security de PostgreSQL.** Hasta que exista, la tenancy es una
      convención que hace cumplir el repositorio, no aislamiento que hace
      cumplir la base de datos. Este es el hueco más importante de esta página.
- [ ] Prefijos de storage por tenant aplicados automáticamente en vez de por
      convención dentro de la clave
- [ ] Uploads y downloads en streaming. `put()` toma bytes; un upload grande
      debería presignar directo a S3 y nunca tocar la aplicación
- [ ] Escaneo de virus para uploads públicos
- [ ] Un envío de FCM verificado contra un proyecto real en CI. La construcción
      del payload está testeada; la entrega no
- [ ] `jfast deploy function` aplicado en CI contra una cuenta real
