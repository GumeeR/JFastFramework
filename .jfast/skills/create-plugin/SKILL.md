---
name: create-plugin
description: Build a plugin that adds a cross-cutting capability — a client,
  middleware, background worker, or new infrastructure dependency.
when_to_use: The capability is optional, is used by more than one service, or
  brings its own dependency — auth, S3 storage, a message queue, a vendor SDK.
when_not_to_use: It is business logic for one entity (use create-module), or
  every service needs it unconditionally (propose a kernel change instead).
---

## Decide first

| Question | If yes |
| --- | --- |
| Does it need a dependency not in the kernel? | plugin, with its own extra |
| Should a service be able to turn it off? | plugin |
| Does it open a connection or pool? | plugin, with `startup` / `shutdown` |
| Does it need a container to run? | plugin, with `infra()` |
| Is it business logic for one entity? | **module**, not a plugin |

## Steps

1. **Create the file** at `src/jfastframework/plugins/builtin/<name>.py` for a
   built-in, or in your own package for a third-party plugin.

2. **Declare settings** with their own env prefix:

   ```python
   class StorageSettings(PluginSettings):
       model_config = SettingsConfigDict(
           env_prefix="JFAST_STORAGE_", env_file=".env", extra="ignore"
       )
       bucket: str
       access_key: SecretStr | None = None
   ```

   Anything secret is `SecretStr`. It is then excluded from
   `jfast describe` and `/info` automatically.

3. **Declare meta.** Be precise — the registry enforces all of it:

   ```python
   meta = PluginMeta(
       name="storage",
       requires=("database",),     # pulled in automatically if missing
       after=("observability",),   # ordering only, no error if absent
       provides=("storage",),      # duplicate claims fail at build time
       default_enabled=False,      # opt-in unless it is universally wanted
       extra="jfastframework[storage]",
   )
   ```

4. **Implement the hooks you need.** All are optional.

   - `register(ctx)` — build time. Mount routers, add middleware, publish
     providers. **No I/O.** It runs before the loop is serving.
   - `startup(ctx)` — open pools and connections.
   - `shutdown(ctx)` — close what `startup` opened. Runs in reverse order.
   - `health(ctx)` — return `HealthReport.ok(...)` or
     `HealthReport.fail(..., critical=False)` when a failure degrades rather
     than breaks the service.
   - `infra(ctx)` — the containers this plugin needs, with a `port_offset`
     inside the service's ten-port block.

5. **Import heavy dependencies inside the method**, never at module top level.
   Discovery imports every plugin class; a top-level import of a missing extra
   makes the plugin unimportable for everyone.

6. **Register the entry point** in `pyproject.toml`:

   ```toml
   [project.entry-points."jfastframework.plugins"]
   storage = "jfastframework.plugins.builtin.storage:StoragePlugin"
   ```

   Reinstall (`pip install -e .`) — entry points are read at install time.

7. **Add the extra** under `[project.optional-dependencies]`.

## Verification

```bash
pip install -e ".[all,dev]"
jfast plugins list --all           # your plugin appears and is importable
jfast doctor
pytest tests/
jfast deploy compose --stdout      # if you implemented infra()
```

Write a test using `NullPlugin` and `build_test_app` from
`jfastframework.testing`. A plugin without a test for its ordering and its
health report is not done.

## Common mistakes

- I/O in `register`. Stalls startup for every service loading the plugin.
- Top-level import of an optional dependency. Breaks discovery.
- Forgetting `provides`. The duplicate-key check cannot protect what it does
  not know about.
- `default_enabled=True` for something not every service wants.
- A `health` that returns `critical=True` for a cache. A cold cache degrades
  the service; it does not take it out of rotation.
