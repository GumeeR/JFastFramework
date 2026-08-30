# JFastFramework — plan para 0.1.0a5

## Contexto

`0.1.0a4` cerró los 23 hallazgos de un reporte externo contra `a3`, más una
auditoría de seguridad y cinco comandos de ciclo de vida. Salió con 1058 tests,
13 smoke scripts y el gate completo en verde.

Un segundo reporte externo, contra `a4`, trae **20 hallazgos**. Verifiqué nueve.
**Las nueve confirmadas, y dos son peores de lo que dice el reporte.**

Su párrafo de cierre es el que da forma a este plan:

> Los diagnósticos nuevos de a4 son lo bastante buenos como para que la gente
> les crea, y eso hace que un diagnóstico que miente salga más caro que el
> defecto que venía a encontrar.

Es exacto. En un día construimos siete comandos de diagnóstico, y tres mienten
en casos concretos. Sin `migration check` uno lee la migración a mano; con uno
que aprueba tres sentencias peligrosas, deja de leerla.

### Cómo se nos escapó, en dos frases

**Verificamos las rutas que arreglamos, no las que usa la gente.**
`contracts init --layout X` en el smoke cuando la gente corre `jfast new
service`. El middleware de proxies aislado, cuando en producción corre detrás de
uvicorn. El pipeline por `put()`, cuando `write()` lo esquiva.

**Y varios tests afirman la mitad que funciona.** El de `events.host_port`
comprueba el string anunciado y nunca el mapping de `ports`. Ese test pasa
mientras el defecto que cubre está presente.

---

## Verificado por mí

| # | Qué | Evidencia |
|---|---|---|
| **1** | **uvicorn pisa los proxies.** Cero `proxy_headers` / `forwarded_allow_ips` en todo el fuente; uvicorn reescribe `scope["client"]` antes que cualquier middleware. `serve` bindea `127.0.0.1`, que es el peer que uvicorn confía por defecto: **se valida en local y sale correcto por el motivo equivocado.** Un `X-Forwarded-Proto` falsificado además voltea `scope["scheme"]`, que es la compuerta de HSTS | grep completo |
| **2** | **El parser no lee su propia plantilla.** `migrations.py:242` y `upgrades.py:266` filtran `ast.Assign`; `script.py.mako.j2:19-20` emite `revision: str = ...` y `down_revision: str \| None = ...`, que son `AnnAssign`. Mismo punto ciego deja invisible un `__tablename__: str = "users"` | lectura directa |
| **4** | **El kernel no tiene default consciente de storage — y su comentario lo admite.** `settings.py:95` fija 2 MiB para todos; `:92` dice *"`service --with storage` writes a bigger number into jfast.toml"*, o sea que el valor grande **solo existe si el scaffold lo escribió**. `upgrades.py:340` lo reporta por plugin habilitado. Habilitar `storage` después de scaffoldear ⇒ runtime 2 MiB, `upgrade` promete 25 | lectura de los tres |
| **5** | **Keyset con columna nullable pierde datos, peor de lo reportado.** No "termina la página antes": se detiene tras la primera y dice que terminó | corrido: **20 de 200 alcanzadas, 180 inalcanzables, `has_more=False`**; offset alcanza 200 |
| **8** | **`new service` siempre escribe el contrato layered.** `scaffold.py:495` hardcodea `contracts_layered`; `CONTRACT_TEMPLATE_FOR` existe en `:42` y su único consumidor es `contracts init` en `main.py:1975` | lectura directa |
| **11** | **El test afirma la mitad que funciona.** `test_host_port_overrides_the_derived_advertised_port` comprueba `EXTERNAL://localhost:9092` en el string anunciado y **nunca** el mapping `ports:` de compose | lectura directa |
| **18** | **`useTheme().resolved` es un `ref` por llamada**, con un comentario encima que afirma la propiedad que no entrega | lectura directa |
| **20a** | `render_dockerfile(python_version, workers=None)` acepta `workers`; `cli/main.py` no expone la bandera | grep |
| **20b** | `settings.py:92` documenta la regla de storage como escritura del scaffold, confirmando que el `4` es diseño y no olvido | lectura |

## Sin verificar por mí

`3` ALTERs a media migración · `6` `on_refresh` · `7` `sys.path` · `9`
`Page.total` fuera del manifiesto · `10` `check` no corre ruff/mypy/pytest ·
`12` los dos generadores de compose · `13` `container_name` · `14` `ratelimit`
invisible · `15` rename con backfill · `16` remedios inválidos · `17` `ai
context` 53% arriba · `19` el 401 del frontend · resto del `20`

**Cada uno se verifica antes de tocarlo.** Del reporte contra `a3`, 2 de 23
estaban mal; asumir 20 de 20 sería el mismo error en la otra dirección.

---

## Lo que cambió al leer la versión larga

**El `4` no es un número mal, es una regla que se planeó y no se implementó.**
El comentario de `settings.py:92` describe el comportamiento deseado —storage
sube el límite— y lo entrega solo el generador. El runtime no lo sabe. Así que
un servicio que en `a3` aceptaba subidas de 3 MB hoy responde 413, y `upgrade
--check` le dice que su límite son 25 MiB. **El arreglo no es tocar el
manifiesto: es decidir dónde vive esa regla.** Si vive en el kernel, el
manifiesto se vuelve cierto solo. Si vive en el scaffold, el manifiesto tiene
que leer el `jfast.toml` real en vez de deducirlo del plugin.

**El `8` dejó `AGENTS.md` contradiciéndose consigo mismo.** En `:50-52` advierte
que *"a passing check is not evidence its layers were looked at"* — texto que
agregamos en `a4` — y en la regla 1 afirma que *"every layout's contract sets
`forbid_packages = ["sqlalchemy"]` on that layer"*. Las dos frases viajan en el
mismo archivo. Arreglar el scaffold vuelve cierta la regla 1 y deja la
advertencia como lo que debe ser: una nota sobre módulos mezclados, no sobre el
caso normal.

**El `11` es el ejemplo canónico de la segunda regla nueva.** No hace falta
buscar más: el test existe, pasa, y comprueba exactamente el lado que no puede
romperse.

---

## Desacuerdos

**El `10` no es defecto, es alcance.** `check` no corre ruff/mypy/pytest a
propósito: esas herramientas ya tienen su comando y meterlas dentro duplica
configuración. Pero el riesgo es real, y el reporte lo mide: inyectar las cuatro
fallas produce salida byte-idéntica. **Se arregla nombrando, no agregando** —
que `check` diga en su ayuda y en su salida qué *no* corre. La combinación
actual de nombre y cobertura es la que no sirve.

**El `9` es justo y lo archivamos mal.** `Page.total: int | None` cambia el
schema de respuesta de todo endpoint paginado generado: afecta clientes, no solo
código que type-checkea. Debió ir en **Rompe** y en `upgrades.py`.

---

## Olas

Mismo reparto que en `a4`: archivos disjuntos, nadie toca `cli/main.py` salvo yo
al cerrar.

### Ola A — mienten, pierden datos, o son seguridad

| Agente | Posee | Arregla |
|---|---|---|
| **A-proxy** | arranque de uvicorn en `cli/main.py`, `cli/dev.py`, `middleware.py`, `deploy/compose.py` (entrypoint) | `1`. `proxy_headers=False` en `serve`, `--no-proxy-headers` en el Dockerfile generado, y que el guard de deferencia detecte la reescritura de uvicorn. **El test corre detrás de uvicorn real**, no contra el middleware aislado. Incluir el `X-Forwarded-Proto` que abre HSTS |
| **A-parsers** | `cli/migrations.py`, `upgrades.py`, tests | `2`. Test que parsee una revisión **generada por la plantilla**, no escrita a mano |
| **A-keyset** | `db/repository.py`, `tests/test_repository.py` | `5`. `NULLS LAST` con nulidad en el cursor, o excepción al construir. Corregir el docstring que suaviza la consecuencia |
| **A-contract** | `cli/scaffold.py`, `templates/agent_docs/AGENTS.md.j2`, `tests/test_scaffold.py`, `scripts/smoke_layouts.sh` | `8`. Que el smoke pase por `new service`. Y que `contracts check` diga **cuántos archivos matcheó cada glob**: una capa con cero es el hallazgo |
| **A-auth** | `plugins/builtin/auth.py`, tests, `docs/auth.md` + es | `6`, más el `sign-out-everywhere` de `tokens.py:151` y la carrera de refresh concurrente del `20` |

### Ola B — bloquean uso

| Agente | Posee | Arregla |
|---|---|---|
| **B-syspath** | entrada de `cli/main.py`, `pyproject.toml`, tests | `7`. Reproducir primero: contra `a3` no lo pude reproducir |
| **B-limits** | `settings.py`, `upgrades.py`, `cli/upgrade.py`, tests | `4`, `3`, `9`. **Decidir dónde vive la regla de storage antes de tocar el manifiesto** |
| **B-compose** | `deploy/`, `resources.py`, `workspace.py`, `plugins/base.py` (`InfraService`), tests | `11`, `12`, `13`. Campo de puerto publicado, prefijo de workspace en `container_name` |
| **B-migration** | `cli/migrations.py`, tests | `15`, `16`. Buscar el backfill entre el add y el drop; `op.execute` cubierto o declarado sin cubrir |

### Ola C — superficie

| Agente | Posee | Arregla |
|---|---|---|
| **C-front** | `templates/frontend_*/`, `scripts/smoke_components.sh` | `18` (computed, vuelta a `system`, scroll lock del drawer), `19` (plantilla de 401 con el bug del header ya resuelto) |
| **C-cli** | `cli/check.py`, `cli/ai.py`, `templates/service_base/jfast.toml.j2`, docs | `10` (nombrar lo que no corre), `14`, `17`, y el resto del `20` |

### Ola D — mía, secuencial

1. Las dos reglas nuevas, como test ejecutable.
2. `0.1.0a5`, changelog EN + ES con `Page.total` en **Rompe**.
3. Gate completo con PostgreSQL y Redis reales.

---

## Las dos reglas que salen de esto

**1. Ningún diagnóstico se da por bueno sin un caso donde debe fallar y falla.**
Tres de estos veinte habrían muerto ahí.

**2. El test tiene que afirmar la mitad que puede romperse, por la ruta que toma
un usuario.** No el string anunciado cuando lo que falla es el mapping. No
`contracts init` cuando la gente corre `new service`. No el middleware aislado
cuando corre detrás de uvicorn.

Van como test, no como nota: por cada comando de diagnóstico, un caso verde y
uno rojo, ambos construidos desde la entrada pública.

---

## Verificación

Por agente: `ruff check` · `ruff format --check` · `mypy src` · `pytest -q`, más
el smoke de lo suyo.

Al cerrar: el gate completo con contenedores reales (`postgres:16` en 5499 con
usuario `jfast`, `redis:7-alpine` en 6399, `JFAST_TEST_PG_URL` y
`JFAST_TEST_REDIS_URL`), y la guarda que **falla si algún test se salta por
falta de servidor** — dos veces en `a4` un gate salió verde con siete skips
silenciosos.
