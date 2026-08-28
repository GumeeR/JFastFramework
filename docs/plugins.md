# Writing a plugin

A plugin is a class with metadata and up to five optional hooks. Nothing else.

## Minimal plugin

```python
from jfastframework import HealthReport, Plugin, PluginMeta


class GreetingPlugin(Plugin):
    meta = PluginMeta(name="greeting", description="Adds /hello.")

    def register(self, ctx):
        @ctx.app.get("/hello")
        async def hello():
            return {"hello": ctx.settings.app_name}
```

Load it without installing anything:

```python
app = create_app(plugins=[GreetingPlugin])
```

## PluginMeta

| Field | Meaning |
| --- | --- |
| `name` | Unique identifier. What goes in `jfast.toml`. |
| `version` | Plugin version, independent of the framework. |
| `description` | One line, shown by `jfast plugins list`. |
| `requires` | Hard dependencies. Pulled in automatically; a cycle is an error. |
| `after` | Soft ordering. Ordered after these if present, no error if absent. |
| `provides` | Context keys published. Two plugins claiming one key fails at build time. |
| `default_enabled` | Loaded when the service pins no explicit allow-list. |
| `extra` | Install extra needed for the plugin to import. |

`requires` vs `after`: `rag` **requires** `database` (it cannot function
without an engine). `metrics` comes **after** `observability` (nicer log
ordering, but it works alone).

## Settings

Each plugin owns its config block, with its own env prefix.

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

Values come from `[plugin.storage]` in `jfast.toml`, overridden by
`JFAST_STORAGE_*` environment variables. Secrets are `SecretStr` and never
appear in `describe()` or `/info`.

## Hooks

```python
def register(self, ctx: AppContext) -> None
```
Build time, before the loop serves. Mount routers, add middleware, publish
providers. **No I/O.** A blocking call here stalls startup.

```python
async def startup(self, ctx: AppContext) -> None
```
Open pools and connections. Runs in dependency order.

```python
async def shutdown(self, ctx: AppContext) -> None
```
Close what `startup` opened. Runs in reverse. A failure here is logged and does
not stop the other plugins from cleaning up.

```python
async def health(self, ctx: AppContext) -> HealthReport
```
Probe the backing resource. Feeds `/ready`.

```python
HealthReport.ok("reachable", latency_ms=3)
HealthReport.fail("redis down", critical=False)   # degraded, still serving
```

```python
def infra(self, ctx: AppContext | None = None) -> list[InfraService]
```
The containers this plugin needs. Consumed by `jfast deploy compose`.

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

`port_offset` must be below 10 — a service owns a block of ten ports.

## Providers

```python
def register(self, ctx):
    client = build_client(self.settings.url)
    ctx.provide("storage", client)
```

Consumers ask for it by key:

```python
storage = ctx.require("storage")
storage = ctx.require("storage", expected=StorageClient)   # runtime type check
```

A missing provider raises `ProviderNotFound` naming every key that *is*
available. Declare the key in `meta.provides` so conflicts are caught before
either plugin runs.

## Optional dependencies

Import them inside the method, never at module top level:

```python
def register(self, ctx):
    import redis.asyncio as aioredis   # top-level would break discovery
    ...
```

Discovery imports every plugin class. A top-level import of a missing extra
makes the plugin unimportable for everyone, not just for services that use it.

## Distributing a third-party plugin

```toml
[project.entry-points."jfastframework.plugins"]
storage = "mypackage.plugin:StoragePlugin"
```

Install it and any JFast service can enable `"storage"` in `jfast.toml`.

Not packaging it? Point at the class directly:

```toml
[plugins.paths]
storage = "myapp.plugins.storage:StoragePlugin"
```

## Replacing a built-in

Disable it and claim the same provider key:

```toml
[plugins]
enabled = ["observability", "my_cache"]
disabled = ["cache"]
```

Because consumers depend on the key and not on the class, nothing downstream
changes.

## Testing

```python
from jfastframework.testing import build_test_app, client_for


async def test_greeting_endpoint():
    app = build_test_app(plugins=["greeting"], extra_plugins=[GreetingPlugin])
    async with client_for(app) as client:
        response = await client.get("/hello")
    assert response.status_code == 200
```

`NullPlugin` records `register` / `startup` / `shutdown` calls — use it to
assert ordering.

## Checklist

- [ ] `meta.provides` lists every key the plugin publishes
- [ ] Optional imports are inside methods
- [ ] `register` does no I/O
- [ ] `shutdown` closes everything `startup` opened
- [ ] `health` distinguishes critical from degraded
- [ ] Secrets typed `SecretStr`
- [ ] Entry point registered and the package reinstalled
- [ ] Test covering ordering and health
