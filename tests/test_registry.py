"""Plugin resolution: ordering, dependencies, conflicts."""

from __future__ import annotations

import pytest

from jfastframework.errors import PluginError
from jfastframework.plugins.base import Plugin, PluginMeta
from jfastframework.plugins.registry import select


class Alpha(Plugin):
    meta = PluginMeta(name="alpha", provides=("alpha",), default_enabled=True)


class Beta(Plugin):
    meta = PluginMeta(name="beta", requires=("alpha",), provides=("beta",))


class Gamma(Plugin):
    meta = PluginMeta(name="gamma", requires=("beta",))


class ConflictingAlpha(Plugin):
    meta = PluginMeta(name="alpha2", provides=("alpha",))


class CycleA(Plugin):
    meta = PluginMeta(name="cycle_a", requires=("cycle_b",))


class CycleB(Plugin):
    meta = PluginMeta(name="cycle_b", requires=("cycle_a",))


AVAILABLE = {
    "alpha": Alpha,
    "beta": Beta,
    "gamma": Gamma,
    "alpha2": ConflictingAlpha,
    "cycle_a": CycleA,
    "cycle_b": CycleB,
}


def names(plugins: list[type[Plugin]]) -> list[str]:
    return [p.meta.name for p in plugins]


def test_dependencies_are_pulled_in_and_ordered_first() -> None:
    resolved = select(AVAILABLE, enabled=["gamma"], disabled=[])
    assert names(resolved) == ["alpha", "beta", "gamma"]


def test_explicit_order_does_not_override_dependency_order() -> None:
    resolved = select(AVAILABLE, enabled=["gamma", "alpha"], disabled=[])
    assert names(resolved).index("alpha") < names(resolved).index("beta")


def test_empty_allowlist_loads_default_enabled_only() -> None:
    resolved = select(AVAILABLE, enabled=[], disabled=[])
    assert names(resolved) == ["alpha"]


def test_disabled_wins_over_default_enabled() -> None:
    resolved = select(AVAILABLE, enabled=[], disabled=["alpha"])
    assert names(resolved) == []


def test_disabling_a_required_plugin_is_an_error() -> None:
    with pytest.raises(PluginError, match="disabled but required"):
        select(AVAILABLE, enabled=["beta"], disabled=["alpha"])


def test_unknown_plugin_lists_what_is_available() -> None:
    with pytest.raises(PluginError, match="Unknown plugin"):
        select(AVAILABLE, enabled=["nope"], disabled=[])


def test_circular_dependency_is_reported() -> None:
    with pytest.raises(PluginError, match="Circular plugin dependency"):
        select(AVAILABLE, enabled=["cycle_a"], disabled=[])


def test_duplicate_provider_is_rejected_at_build_time() -> None:
    from jfastframework.plugins import registry
    from jfastframework.testing import make_config

    config = make_config(plugins=["alpha", "alpha2"])
    original = registry.discover
    registry.discover = lambda extra_paths=None: dict(AVAILABLE)  # type: ignore[assignment]
    try:
        with pytest.raises(PluginError, match="claimed by both"):
            registry.build(config)
    finally:
        registry.discover = original  # type: ignore[assignment]
