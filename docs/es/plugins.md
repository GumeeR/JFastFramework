# Escribir un plugin

Un plugin es una clase con metadata y hasta cinco hooks opcionales. Nada más.

## Plugin mínimo

```python
from jfastframework import HealthReport, Plugin, PluginMeta


class GreetingPlugin(Plugin):
    meta = PluginMeta(name="greeting", description="Adds /hello.")

    def register(self, ctx):
        @ctx.app.get("/hello")
        async def hello():
            return {"hello": ctx.settings.app_name}
```

Cárgalo sin instalar nada:

```python
app = create_app(plugins=[GreetingPlugin])
```

## PluginMeta

| Campo | Significado |
| --- | --- |
| `name` | Identificador único. Lo que va en `jfast.toml`. |
| `version` | Versión del plugin, independiente de la del framework. |
| `description` | Una línea, la que muestra `jfast plugins list`. |
| `requires` | Dependencias duras. Se cargan solas; un ciclo es un error. |
| `after` | Orden suave. Va después de estos si están, sin error si no. |
| `provides` | Claves de contexto publicadas. Dos plugins reclamando una misma clave falla en build time. |
| `default_enabled` | Se carga cuando el servicio no fija una allow-list explícita. |
| `extra` | Install extra que el plugin necesita para importar. |

`requires` vs `after`: `rag` **requires** `database` (no puede funcionar sin un
engine). `metrics` va **after** `observability` (ordena mejor los logs, pero
funciona solo).

## Settings

Cada plugin es dueño de su bloque de config, con su propio prefijo de env.

```python
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict
from jfastframework import PluginSettings


class StorageSettings(PluginSettings):
    model_config = SettingsConfigDict(
        env_prefix="JFAST_STORAGE_", env_file=".env", extra="ignore"
    )
    bucket: str = "uploads"
    access_key: SecretStr | None = None
```

Los valores salen de `[plugin.storage]` en `jfast.toml`, pisados por las
variables de entorno `JFAST_STORAGE_*`. Los secretos son `SecretStr` y nunca
aparecen en `describe()` ni en `/info`.

## Hooks

```python
def register(self, ctx: AppContext) -> None
```
Build time, antes de que el loop atienda. Monta routers, agrega middleware,
publica providers. **Sin I/O.** Una llamada bloqueante aquí frena el arranque.

```python
async def startup(self, ctx: AppContext) -> None
```
Abre pools y conexiones. Corre en orden de dependencias.

```python
async def shutdown(self, ctx: AppContext) -> None
```
Cierra lo que abrió `startup`. Corre en orden inverso. Una falla aquí se
loguea y no impide que los otros plugins limpien lo suyo.

```python
async def health(self, ctx: AppContext) -> HealthReport
```
Sondea el recurso de atrás. Alimenta `/ready`.

```python
HealthReport.ok("reachable", latency_ms=3)
HealthReport.fail("redis down", critical=False)   # degraded, still serving
```

```python
def infra(self, ctx: AppContext | None = None) -> list[InfraService]
```
Los contenedores que este plugin necesita. Los consume `jfast deploy compose`.

```python
InfraService(
    name="redis",
    image="redis:7-alpine",
    port_offset=3,           # host port = service base port + 3
    internal_port=6379,
    volumes=["redis_data:/data"],
    healthcheck={"test": ["CMD", "redis-cli", "ping"], "interval": "5s"},
)
```

`port_offset` tiene que ser menor a 10 — un servicio es dueño de un bloque de
diez puertos.

## Providers

```python
def register(self, ctx):
    client = build_client(self.settings.url)
    ctx.provide("storage", client)
```

Los consumidores lo piden por clave:

```python
storage = ctx.require("storage")
storage = ctx.require("storage", expected=StorageClient)   # runtime type check
```

Un provider que falta levanta `ProviderNotFound` nombrando todas las claves que
*sí* están disponibles. Declara la clave en `meta.provides` para que los
conflictos se detecten antes de que corra cualquiera de los dos plugins.

## Dependencias opcionales

Impórtalas adentro del método, nunca en el top level del módulo:

```python
def register(self, ctx):
    import redis.asyncio as aioredis   # top-level would break discovery
    ...
```

El discovery importa todas las clases de plugin. Un import de un extra que
falta, en el top level, deja el plugin imposible de importar para todos, no
solo para los servicios que lo usan.

## Distribuir un plugin de terceros

```toml
[project.entry-points."jfastframework.plugins"]
storage = "mypackage.plugin:StoragePlugin"
```

Instálalo y cualquier servicio JFast puede habilitar `"storage"` en
`jfast.toml`.

¿No lo vas a empaquetar? Apunta directo a la clase:

```toml
[plugins.paths]
storage = "myapp.plugins.storage:StoragePlugin"
```

## Reemplazar uno de los que vienen incluidos

Deshabilítalo y reclama la misma clave de provider:

```toml
[plugins]
enabled = ["observability", "my_cache"]
disabled = ["cache"]
```

Como los consumidores dependen de la clave y no de la clase, nada aguas abajo
cambia.

## Testing

```python
from jfastframework.testing import build_test_app, client_for


async def test_greeting_endpoint():
    app = build_test_app(plugins=["greeting"], extra_plugins=[GreetingPlugin])
    async with client_for(app) as client:
        response = await client.get("/hello")
    assert response.status_code == 200
```

`NullPlugin` registra las llamadas a `register` / `startup` / `shutdown` —
úsalo para verificar el orden.

## Checklist

- [ ] `meta.provides` lista todas las claves que publica el plugin
- [ ] Los imports opcionales están adentro de los métodos
- [ ] `register` no hace I/O
- [ ] `shutdown` cierra todo lo que abrió `startup`
- [ ] `health` distingue crítico de degradado
- [ ] Secretos tipados `SecretStr`
- [ ] Entry point registrado y el paquete reinstalado
- [ ] Test que cubra orden y health
