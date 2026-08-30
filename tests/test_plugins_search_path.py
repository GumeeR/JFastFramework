"""`discover` resolving `[plugins.paths]`: what it imports and what it tolerates.

The subprocess suite next door proves the console script agrees with `python
-m`. These are the same rules at unit range, where the failure is readable.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from jfastframework.plugins import registry

PLUGIN_MODULE = """\
from __future__ import annotations

from jfastframework.plugins.base import Plugin, PluginMeta


class SocialRuntimePlugin(Plugin):
    meta = PluginMeta(name="social_runtime", version="1.0.0")
"""


@pytest.fixture(autouse=True)
def restore_import_state() -> Iterator[None]:
    """`discover` mutates `sys.path` and imports; neither may leak into the next test."""
    before = list(sys.path)
    modules = set(sys.modules)
    yield
    sys.path[:] = before
    for name in set(sys.modules) - modules:
        del sys.modules[name]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    package = tmp_path / "social_plugins"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "runtime.py").write_text(PLUGIN_MODULE, encoding="utf-8")
    return tmp_path


PATHS = {"social_runtime": "social_plugins.runtime:SocialRuntimePlugin"}


def test_a_project_local_plugin_resolves_from_the_search_path(project: Path) -> None:
    found = registry.discover(extra_paths=PATHS, search_path=project)
    assert found["social_runtime"].meta.name == "social_runtime"


def test_the_search_path_goes_to_the_front(project: Path) -> None:
    registry.discover(extra_paths=PATHS, search_path=project)
    assert sys.path[0] == str(project.resolve())


def test_it_is_not_inserted_twice(project: Path) -> None:
    registry.discover(extra_paths=PATHS, search_path=project)
    registry.discover(extra_paths=PATHS, search_path=project)
    assert sys.path.count(str(project.resolve())) == 1


def test_sys_path_is_left_alone_when_nothing_declares_a_path() -> None:
    before = list(sys.path)
    registry.discover()
    assert sys.path == before


def test_an_unimportable_path_is_recorded_rather_than_raised(tmp_path: Path) -> None:
    """The crash that took `workspace compose` down with it."""
    found = registry.discover(
        extra_paths={"social_runtime": "nowhere_at_all.runtime:Plugin"}, search_path=tmp_path
    )

    assert "social_runtime" not in found
    assert "nowhere_at_all" in registry.discover.broken["social_runtime"]  # type: ignore[attr-defined]


def test_a_name_the_plugin_disagrees_with_is_recorded_too(project: Path) -> None:
    found = registry.discover(
        extra_paths={"typo": "social_plugins.runtime:SocialRuntimePlugin"}, search_path=project
    )

    assert "typo" not in found
    assert "registers it as 'typo'" in registry.discover.broken["typo"]  # type: ignore[attr-defined]
