"""Deploy artifacts are derived from the plugin graph, not hand-written."""

from __future__ import annotations

from typing import Any

import pytest

from jfastframework.deploy import build_compose, render_compose
from jfastframework.plugins.base import InfraService, PluginMeta
from jfastframework.testing import NullPlugin, make_config


class WithRedis(NullPlugin):
    meta = PluginMeta(name="with_redis")

    def infra(self, ctx: Any = None) -> list[InfraService]:
        return [
            InfraService(
                name="redis",
                image="redis:7-alpine",
                port_offset=3,
                internal_port=6379,
                volumes=["redis_data:/data"],
            )
        ]


class OutOfBlock(NullPlugin):
    meta = PluginMeta(name="out_of_block")

    def infra(self, ctx: Any = None) -> list[InfraService]:
        return [InfraService(name="bad", image="x", port_offset=11, internal_port=1)]


def test_disabled_plugin_contributes_no_container() -> None:
    config = make_config(port=8010)
    compose = build_compose(config, [])
    assert set(compose["services"]) == {"api"}


def test_enabled_plugin_contributes_its_container_and_volume() -> None:
    config = make_config(port=8010)
    compose = build_compose(config, [WithRedis()])
    assert compose["services"]["redis"]["ports"] == ["8013:6379"]
    assert "redis_data" in compose["volumes"]


def test_port_offsets_are_relative_to_the_service_base_port() -> None:
    config = make_config(port=8050)
    compose = build_compose(config, [WithRedis()])
    assert compose["services"]["redis"]["ports"] == ["8053:6379"]


def test_offset_outside_the_port_block_is_rejected() -> None:
    config = make_config(port=8010)
    with pytest.raises(ValueError, match="outside the 10-port block"):
        build_compose(config, [OutOfBlock()])


def test_rendered_compose_is_valid_yaml() -> None:
    yaml = pytest.importorskip("yaml")
    config = make_config(app_name="billing", port=8010)
    rendered = render_compose(build_compose(config, [WithRedis()]))
    parsed = yaml.safe_load(rendered)
    assert parsed["services"]["redis"]["image"] == "redis:7-alpine"
    assert parsed["services"]["api"]["container_name"] == "billing_api"
