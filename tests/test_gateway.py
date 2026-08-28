"""Gateway: what it proxies, what it refuses to swallow, and how it fails."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Request

from jfastframework.plugins.builtin.gateway import GatewayRoute, GatewaySettings
from jfastframework.testing import build_test_app, client_for


def upstream_app():  # type: ignore[no-untyped-def]
    """A tiny service to proxy to, mounted in-process."""
    router = APIRouter()

    @router.get("/echo")
    async def echo(request: Request) -> dict[str, object]:
        return {
            "path": request.url.path,
            "query": dict(request.query_params),
            "request_id": request.headers.get("X-Request-ID"),
            "forwarded_host": request.headers.get("X-Forwarded-Host"),
        }

    @router.post("/echo")
    async def echo_body(payload: dict[str, object]) -> dict[str, object]:
        return {"received": payload}

    # Also mounted under the prefix, for the strip_prefix=False case: an
    # upstream that already namespaces its own routes.
    @router.get("/billing/echo")
    async def echo_prefixed(request: Request) -> dict[str, object]:
        return {"path": request.url.path}

    return build_test_app(app_name="upstream", routers=[router])


def gateway_app(routes: list[dict[str, object]]):  # type: ignore[no-untyped-def]
    return build_test_app(
        plugins=["gateway"],
        app_name="gateway",
        raw={"plugin": {"gateway": {"routes": routes, "timeout": 2.0}}},
    )


def wire(gateway, upstream) -> None:  # type: ignore[no-untyped-def]
    """Point the gateway's httpx client at the in-process upstream app."""
    import httpx
    from httpx import ASGITransport

    ctx = gateway.state.jfast
    old = ctx.get("gateway.client")
    plugin = next(p for p in gateway.state.plugins if p.meta.name == "gateway")
    plugin._client = httpx.AsyncClient(transport=ASGITransport(app=upstream), timeout=2.0)
    assert old is not None


# -- configuration validation ------------------------------------------


def test_a_root_prefix_is_refused() -> None:
    # "/" would swallow /health, /ready and /metrics on the gateway itself.
    with pytest.raises(ValueError, match="would swallow"):
        GatewayRoute(prefix="/", target="http://x:1")


def test_a_prefix_must_be_a_path() -> None:
    with pytest.raises(ValueError, match="must start with"):
        GatewayRoute(prefix="billing", target="http://x:1")


def test_a_target_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute http"):
        GatewayRoute(prefix="/billing", target="billing:8010")


def test_trailing_slashes_are_normalised() -> None:
    route = GatewayRoute(prefix="/billing/", target="http://billing:8010/")
    assert route.prefix == "/billing"
    assert route.target == "http://billing:8010"


def test_routes_are_read_from_the_plugin_config_block() -> None:
    settings = GatewaySettings(
        routes=[{"prefix": "/billing", "target": "http://billing:8010"}]  # type: ignore[list-item]
    )
    assert settings.routes[0].prefix == "/billing"


# -- routing behaviour --------------------------------------------------


async def test_the_gateway_keeps_its_own_system_endpoints() -> None:
    app = gateway_app([{"prefix": "/billing", "target": "http://billing:8010"}])
    async with client_for(app) as client:
        health = await client.get("/health")
        routes = await client.get("/gateway/routes")

    # If the gateway registered a catch-all, /health would be proxied instead.
    assert health.status_code == 200
    assert health.json()["service"] == "gateway"
    assert routes.json()["routes"][0]["prefix"] == "/billing"


async def test_a_request_is_forwarded_with_the_prefix_stripped() -> None:
    upstream = upstream_app()
    gateway = gateway_app([{"prefix": "/billing", "target": "http://billing"}])

    async with client_for(upstream), client_for(gateway) as client:
        wire(gateway, upstream)
        response = await client.get("/billing/echo", params={"page": "2"})

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "/echo"
    assert body["query"] == {"page": "2"}


async def test_the_prefix_is_kept_when_strip_prefix_is_off() -> None:
    upstream = upstream_app()
    gateway = gateway_app(
        [{"prefix": "/billing", "target": "http://billing", "strip_prefix": False}]
    )

    async with client_for(upstream), client_for(gateway) as client:
        wire(gateway, upstream)
        response = await client.get("/billing/echo")

    assert response.json()["path"] == "/billing/echo"


async def test_the_request_id_survives_the_hop() -> None:
    upstream = upstream_app()
    gateway = gateway_app([{"prefix": "/billing", "target": "http://billing"}])

    async with client_for(upstream), client_for(gateway) as client:
        wire(gateway, upstream)
        response = await client.get("/billing/echo", headers={"X-Request-ID": "trace-me"})

    # Without this, a trace stops at the gateway and the upstream logs are
    # impossible to correlate with the client's request.
    assert response.json()["request_id"] == "trace-me"


async def test_a_body_is_forwarded() -> None:
    upstream = upstream_app()
    gateway = gateway_app([{"prefix": "/billing", "target": "http://billing"}])

    async with client_for(upstream), client_for(gateway) as client:
        wire(gateway, upstream)
        response = await client.post("/billing/echo", json={"amount": 10})

    assert response.json() == {"received": {"amount": 10}}


async def test_an_unreachable_upstream_becomes_502_problem_json() -> None:
    gateway = gateway_app([{"prefix": "/billing", "target": "http://127.0.0.1:1"}])

    async with client_for(gateway) as client:
        response = await client.get("/billing/anything")

    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "Bad Gateway"


async def test_an_unrouted_prefix_is_a_plain_404() -> None:
    gateway = gateway_app([{"prefix": "/billing", "target": "http://billing"}])

    async with client_for(gateway) as client:
        response = await client.get("/unknown/thing")

    assert response.status_code == 404


async def test_readiness_does_not_probe_upstreams() -> None:
    # A gateway whose upstream is restarting is still doing its job; cascading
    # readiness failures would take the whole system out together.
    gateway = gateway_app([{"prefix": "/billing", "target": "http://127.0.0.1:1"}])

    async with client_for(gateway) as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json()["checks"]["gateway"]["healthy"] is True
