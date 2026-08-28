"""Plugin discovery, selection and ordering.

Discovery sources, in precedence order:

1. Plugin classes passed directly to ``create_app`` (tests, embedding).
2. Dotted paths listed in ``jfast.toml`` -- third-party plugins not installed
   as distributions.
3. ``jfastframework.plugins`` entry points -- how built-ins and installed
   third-party packages register themselves.

Selection: an explicit ``plugins`` allow-list wins; otherwise every discovered
plugin with ``default_enabled`` is loaded. ``disabled_plugins`` always wins,
which is the supported way to strip monitoring out of a service.
"""

from __future__ import annotations

import importlib
from importlib import metadata
from typing import TYPE_CHECKING, Any

from jfastframework.errors import PluginError
from jfastframework.plugins.base import Plugin

if TYPE_CHECKING:
    from jfastframework.settings import JFastConfig

ENTRY_POINT_GROUP = "jfastframework.plugins"


def _load_dotted(path: str) -> type[Plugin]:
    module_path, _, attr = path.rpartition(":")
    if not module_path:
        module_path, _, attr = path.rpartition(".")
    if not module_path or not attr:
        raise PluginError(f"Invalid plugin path {path!r}. Expected 'package.module:ClassName'.")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise PluginError(f"Cannot import plugin module {module_path!r}: {exc}") from exc
    try:
        obj = getattr(module, attr)
    except AttributeError as exc:
        raise PluginError(f"Module {module_path!r} has no attribute {attr!r}") from exc
    if not (isinstance(obj, type) and issubclass(obj, Plugin)):
        raise PluginError(f"{path!r} is not a Plugin subclass")
    return obj


def discover(extra_paths: dict[str, str] | None = None) -> dict[str, type[Plugin]]:
    """Return every discoverable plugin class, keyed by plugin name.

    A plugin whose optional dependency is missing is skipped rather than
    crashing discovery -- it only errors if the service actually enables it.
    """
    found: dict[str, type[Plugin]] = {}
    broken: dict[str, str] = {}

    for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            cls = entry_point.load()
        except Exception as exc:  # noqa: BLE001 - optional extras may be absent
            broken[entry_point.name] = str(exc)
            continue
        if isinstance(cls, type) and issubclass(cls, Plugin):
            found[cls.meta.name] = cls

    for name, path in (extra_paths or {}).items():
        cls = _load_dotted(path)
        if cls.meta.name != name:
            raise PluginError(
                f"Plugin at {path!r} is named {cls.meta.name!r}, "
                f"but jfast.toml registers it as {name!r}."
            )
        found[name] = cls

    discover.broken = broken  # type: ignore[attr-defined]
    return found


def select(
    available: dict[str, type[Plugin]],
    *,
    enabled: list[str],
    disabled: list[str],
) -> list[type[Plugin]]:
    """Resolve the plugin allow-list into concrete classes."""
    disabled_set = set(disabled)

    if enabled:
        missing = [name for name in enabled if name not in available]
        if missing:
            broken = getattr(discover, "broken", {})
            hints = []
            for name in missing:
                if name in broken:
                    hints.append(f"{name} (failed to import: {broken[name]})")
                else:
                    hints.append(name)
            raise PluginError(
                "Unknown plugin(s): "
                + ", ".join(hints)
                + ". Available: "
                + (", ".join(sorted(available)) or "<none>")
            )
        chosen = [name for name in enabled if name not in disabled_set]
    else:
        chosen = [
            name
            for name, cls in available.items()
            if cls.meta.default_enabled and name not in disabled_set
        ]

    # Pull in hard dependencies that the allow-list forgot, so enabling `rag`
    # does not fail just because nobody remembered it needs `database`.
    resolved: list[str] = []
    seen: set[str] = set()

    def add(name: str, trail: tuple[str, ...]) -> None:
        if name in seen:
            return
        if name in trail:
            cycle = " -> ".join((*trail, name))
            raise PluginError(f"Circular plugin dependency: {cycle}")
        cls = available.get(name)
        if cls is None:
            raise PluginError(
                f"Plugin {trail[-1] if trail else '?'!r} requires {name!r}, which is not "
                f"installed. Install it or disable the dependent plugin."
            )
        if name in disabled_set:
            raise PluginError(
                f"Plugin {name!r} is disabled but required by "
                f"{trail[-1] if trail else 'the allow-list'!r}."
            )
        for dep in cls.meta.requires:
            add(dep, (*trail, name))
        for soft in cls.meta.after:
            if soft in available and soft not in disabled_set and soft in chosen:
                add(soft, (*trail, name))
        seen.add(name)
        resolved.append(name)

    for name in chosen:
        add(name, ())

    return [available[name] for name in resolved]


def build(
    config: JFastConfig,
    extra_plugins: list[type[Plugin]] | None = None,
) -> list[Plugin]:
    """Discover, select, order and instantiate the plugin graph."""
    settings = config.settings
    paths: dict[str, Any] = config.raw.get("plugins", {}).get("paths", {})

    available = discover(extra_paths=paths)
    for cls in extra_plugins or []:
        available[cls.meta.name] = cls

    ordered = select(
        available,
        enabled=list(settings.plugins),
        disabled=list(settings.disabled_plugins),
    )

    instances = [cls(config.plugin_config(cls.meta.name)) for cls in ordered]

    # Two plugins publishing the same provider key is a config error, and it is
    # much cheaper to catch here than at the first ambiguous `ctx.require`.
    claimed: dict[str, str] = {}
    for plugin in instances:
        for key in plugin.meta.provides:
            if key in claimed:
                raise PluginError(
                    f"Provider {key!r} claimed by both {claimed[key]!r} and "
                    f"{plugin.meta.name!r}. Disable one in jfast.toml."
                )
            claimed[key] = plugin.meta.name

    return instances
