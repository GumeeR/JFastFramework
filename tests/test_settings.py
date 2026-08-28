"""Config loading: file keys, precedence, plugin blocks."""

from __future__ import annotations

from pathlib import Path

from jfastframework.settings import JFastConfig

CONFIG = """
[app]
name = "billing"
port = 8010
env = "staging"

[plugins]
enabled = ["observability", "metrics"]
disabled = ["sentry"]

[plugin.metrics]
path = "/internal/metrics"
"""


def write(tmp_path: Path, body: str = CONFIG) -> Path:
    path = tmp_path / "jfast.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_app_name_key_maps_onto_the_app_name_field(tmp_path: Path) -> None:
    config = JFastConfig.load(config_path=write(tmp_path))
    assert config.settings.app_name == "billing"


def test_scalar_app_keys_are_loaded(tmp_path: Path) -> None:
    config = JFastConfig.load(config_path=write(tmp_path))
    assert config.settings.port == 8010
    assert config.settings.env == "staging"


def test_plugin_lists_are_loaded(tmp_path: Path) -> None:
    config = JFastConfig.load(config_path=write(tmp_path))
    assert config.settings.plugins == ["observability", "metrics"]
    assert config.settings.disabled_plugins == ["sentry"]


def test_plugin_block_is_returned_per_plugin(tmp_path: Path) -> None:
    config = JFastConfig.load(config_path=write(tmp_path))
    assert config.plugin_config("metrics") == {"path": "/internal/metrics"}
    assert config.plugin_config("absent") == {}


def test_overrides_beat_the_config_file(tmp_path: Path) -> None:
    config = JFastConfig.load(config_path=write(tmp_path), overrides={"port": 9999})
    assert config.settings.port == 9999


def test_missing_config_file_falls_back_to_defaults(tmp_path: Path) -> None:
    config = JFastConfig.load(config_path=tmp_path / "nope.toml")
    assert config.settings.app_name == "jfast-service"
    assert config.raw == {}
