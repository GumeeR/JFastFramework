"""Remembering which layout a module was generated with.

The write side is the easy half. The half worth testing is that writing it
twice does not produce two ``[modules.order]`` tables -- that is a TOML parse
error, so the failure would not be a wrong answer from this module but a
service that no longer boots.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from jfastframework.cli import modules
from jfastframework.settings import JFastConfig

BASE = """[app]
name = "shop"
port = 8000

[plugins]
enabled = ["observability"]
"""


@pytest.fixture
def service(tmp_path: Path) -> Path:
    (tmp_path / "jfast.toml").write_text(BASE, encoding="utf-8")
    return tmp_path


def test_a_recorded_layout_reads_back(service: Path) -> None:
    assert modules.record(service, "order", layout="hexagonal", ui="api") is True
    assert modules.layout_of(service, "order") == "hexagonal"


def test_recording_twice_does_not_duplicate_the_table(service: Path) -> None:
    """Two tables of the same name is a parse error, not a duplicate entry."""
    modules.record(service, "order", layout="layered", ui="api")
    modules.record(service, "order", layout="layered", ui="api")

    body = (service / "jfast.toml").read_text(encoding="utf-8")
    assert body.count("[modules.order]") == 1
    tomllib.loads(body)  # would raise on a duplicate


def test_a_changed_layout_replaces_the_old_one(service: Path) -> None:
    modules.record(service, "order", layout="layered", ui="api")
    modules.record(service, "order", layout="screaming", ui="htmx")

    assert modules.layout_of(service, "order") == "screaming"
    assert modules.read_all(service)["order"]["ui"] == "htmx"


def test_several_modules_coexist(service: Path) -> None:
    modules.record(service, "order", layout="layered", ui="api")
    modules.record(service, "billing", layout="modular", ui="api")
    modules.record(service, "audit", layout="hexagonal", ui="api")

    recorded = modules.read_all(service)
    assert {name: entry["layout"] for name, entry in recorded.items()} == {
        "order": "layered",
        "billing": "modular",
        "audit": "hexagonal",
    }


def test_the_runtime_config_still_loads(service: Path) -> None:
    """The table is CLI bookkeeping; the service must not care that it is there."""
    modules.record(service, "order", layout="hexagonal", ui="api")

    config = JFastConfig.load(service / "jfast.toml")
    assert config.settings.app_name == "shop"
    assert "modules" in config.raw


def test_an_unrecorded_module_is_none_not_a_guess(service: Path) -> None:
    assert modules.layout_of(service, "never-made") is None


def test_no_config_file_is_not_an_error(tmp_path: Path) -> None:
    """`jfast new module` outside a service should still write the files."""
    assert modules.read_all(tmp_path) == {}
    assert modules.layout_of(tmp_path, "order") is None
    assert modules.record(tmp_path, "order", layout="layered", ui="api") is False


def test_a_malformed_config_reads_as_empty(tmp_path: Path) -> None:
    """A broken jfast.toml is the config loader's error to report, not this one's."""
    (tmp_path / "jfast.toml").write_text("[app\nname =", encoding="utf-8")
    assert modules.read_all(tmp_path) == {}
