"""Kernel behaviour: lifespan ordering, system endpoints, error shape."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter

from jfastframework.context import ProviderNotFound
from jfastframework.errors import NotFoundError
from jfastframework.plugins.base import HealthReport, PluginMeta
from jfastframework.testing import NullPlugin, build_test_app, client_for


class Alpha(NullPlugin):
    meta = PluginMeta(name="alpha", provides=("alpha",))


class Beta(NullPlugin):
    meta = PluginMeta(name="beta", requires=("alpha",), provides=("beta",))


class Unhealthy(NullPlugin):
    meta = PluginMeta(name="unhealthy")

    async def health(self, ctx: Any) -> HealthReport:
        return HealthReport.fail("backing store down")


class Degraded(NullPlugin):
    meta = PluginMeta(name="degraded")

    async def health(self, ctx: Any) -> HealthReport:
        return HealthReport.fail("cache down", critical=False)


async def test_health_is_cheap_and_always_ok() -> None:
    app = build_test_app(plugins=["alpha"], extra_plugins=[Alpha])
    async with client_for(app) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_ready_fails_when_a_critical_plugin_is_unhealthy() -> None:
    app = build_test_app(plugins=["unhealthy"], extra_plugins=[Unhealthy])
    async with client_for(app) as client:
        response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


async def test_ready_reports_degraded_for_non_critical_failures() -> None:
    app = build_test_app(plugins=["degraded"], extra_plugins=[Degraded])
    async with client_for(app) as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


async def test_lifespan_starts_in_order_and_shuts_down_in_reverse() -> None:
    app = build_test_app(plugins=["beta"], extra_plugins=[Alpha, Beta])
    plugins = app.state.plugins
    assert [p.meta.name for p in plugins] == ["alpha", "beta"]

    async with client_for(app) as client:
        await client.get("/health")

    alpha, beta = plugins
    assert alpha.events == ["register", "startup", "shutdown"]
    assert beta.events == ["register", "startup", "shutdown"]


async def test_domain_errors_serialise_as_problem_json() -> None:
    router = APIRouter()

    @router.get("/boom")
    async def boom() -> None:
        raise NotFoundError("widget 7 not found")

    app = build_test_app(routers=[router])
    async with client_for(app) as client:
        response = await client.get("/boom")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["title"] == "Not Found"
    assert body["detail"] == "widget 7 not found"
    assert body["instance"] == "/boom"


def test_require_missing_provider_names_the_alternatives() -> None:
    app = build_test_app(plugins=["alpha"], extra_plugins=[Alpha])
    ctx = app.state.jfast
    with pytest.raises(ProviderNotFound, match="Available providers: alpha"):
        ctx.require("cache")


async def test_info_endpoint_is_absent_in_production() -> None:
    app = build_test_app(env="prod")
    async with client_for(app) as client:
        response = await client.get("/info")
    assert response.status_code == 404
