# Migraciones y tests

Las dos vienen cableadas en cada servicio generado. Ninguna es algo que tengas
que configurar.

---

## Migraciones (Alembic)

`jfast new service` escribe `alembic.ini`, `migrations/env.py`,
`migrations/script.py.mako` y `migrations/versions/` siempre que el plugin
`database` esté habilitado.

```bash
alembic revision --autogenerate -m "add invoices"
alembic upgrade head
alembic downgrade -1
alembic history
```

Dos cosas que hace y que un `alembic init` de fábrica no hace:

**El DSN sale de la configuración de la aplicación.** `env.py` lee
`JFAST_DB_DSN` a través del mismo `DatabaseSettings` que usa el servicio, y
`alembic.ini` deliberadamente no tiene `sqlalchemy.url`. Una migración que
*puede* correr contra una base de datos distinta a la del servicio en algún
momento lo va a hacer, en el peor momento posible.

**Los modelos se importan automáticamente.** Autogenerate solo ve las tablas
cuyas clases fueron importadas. Un import olvidado produce una migración vacía,
y la tabla que falta se descubre en producción. `env.py` recorre `modules/` e
importa el `models.py` de cada módulo (layout layered) o su `storage.py`
(layout screaming), así que los dos funcionan sin que mantengas una lista de
imports.

También habilita `compare_type` y `compare_server_default` — sin ellos
autogenerate se pierde en silencio los cambios de tipo de columna y los cambios
de default, las dos ediciones que la gente más suele dar por detectadas.

### Los nombres de constraints están fijados

`jfastframework.db.Base` define un `naming_convention`. Sin él PostgreSQL
inventa los nombres de las constraints y autogenerate produce diffs distintos
en máquinas distintas. Con él, una primary key siempre es `pk_<table>`, y una
foreign key siempre `fk_<table>_<column>_<referred>`.

Adoptarlo en una base de datos que ya tiene constraints con nombres automáticos
requiere una migración única. Hazla antes de que la flota crezca.

### Lee la migración antes de aplicarla

Autogenerate es un borrador, no un plan:

- Un **rename** se renderiza como un drop más un add. En una tabla con filas,
  eso es pérdida de datos silenciosa.
- Las **migraciones de datos** no se escriben en absoluto.
- Los renombres de índices y los cambios de enum se pierden con frecuencia.

### SQL offline

```bash
alembic upgrade head --sql > migration.sql
```

Corre `env.py` sin conectarse, que es además la forma en que CI verifica el
cableado sin una base de datos.

---

## Tests (pytest)

`jfast new service` escribe `pytest.ini` y `conftest.py`. Los módulos generados
traen sus propios tests, que pasan de inmediato:

```bash
pytest                          # modules/ and tests/
pytest modules/invoice/tests    # one module
```

### Fixtures

`conftest.py` provee `app` y `client`, construidos con una lista **explícita**
de plugins:

```python
@pytest.fixture
def app():
    return build_test_app(plugins=["observability"], app_name="billing")
```

Aquí lo explícito le gana a lo implícito: un test que nombra sus plugins no se
puede romper porque alguien cambió un default en `jfast.toml`.

De `jfastframework.testing`:

| Helper | Qué hace |
| --- | --- |
| `build_test_app(...)` | App con una lista nombrada de plugins, sin archivo de config, sin env |
| `client_for(app)` | Cliente HTTP async con el lifespan realmente ejecutado |
| `NullPlugin` | Registra `register` / `startup` / `shutdown`, para tests de orden |
| `make_config(...)` | Un `JFastConfig` sin tocar el disco |

`client_for` importa más de lo que parece: los plugins que abren recursos en
`startup` necesitan que el lifespan corra, y un `TestClient` pelado se lo salta
en contextos async.

### Qué prueban realmente los tests generados

**Layout layered** — tests a nivel de servicio contra un repositorio falso. Sin
base de datos, sin contenedores.

**Layout screaming** — dos archivos, separados a propósito:

- `test_<module>_domain.py` — la entidad y sus reglas. Sin base de datos, sin
  fakes, sin event loop. Si un test de aquí alguna vez necesita un fixture, una
  regla se escapó del dominio.
- `test_<module>_use_cases.py` — un repositorio falso y un event loop, nada
  más.

Los dos se marcan explícitamente con `pytest.mark.asyncio` en vez de depender
de `asyncio_mode = auto`, así pasan en un proyecto que no configuró
pytest-asyncio.

### Tests de integración

Los tests que necesitan un PostgreSQL o un Redis real no se generan. Márcalos
con `@pytest.mark.integration` y mantenlos fuera de la suite rápida; un harness
de integración a nivel de framework es la fase 6 de PLAN.md.

---

## Qué debería correr CI

```bash
pytest
ruff check src tests
mypy src
jfast doctor
```

Además, para cualquier cosa que toque templates:

```bash
bash scripts/smoke.sh              # renders both layouts, runs their tests, checks alembic
bash scripts/smoke_workspace.sh    # workspace, gateway, frontends, patching
```

Los templates son la parte que se rompe en silencio — renderizan bien y
producen código que no importa. `pytest` por sí solo no atrapa eso.
