"""Gateway: what it proxies, what it refuses to swallow, and how it fails."""

from __future__ import annotations

import gzip

import pytest
from fastapi import APIRouter, Request, Response

from jfastframework.plugins.builtin.gateway import GatewayRoute, GatewaySettings
from jfastframework.testing import build_test_app, client_for


def upstream_app():  # type: ignore[no-untyped-def]
    """A tiny service to proxy to, mounted in-process."""
    router = APIRouter()

    @router.get("/echo")
    async def echo(request: Request) -> dict[str, object]:
        return {
            "path": request.url.path,
            # multi_items(), so a proxy that drops a repeated value fails here
            # rather than being agreed with: the obvious dict() spelling makes
            # this endpoint blind to exactly the bug it exists to catch.
            "query": request.query_params.multi_items(),
            "request_id": request.headers.get("X-Request-ID"),
            "forwarded_host": request.headers.get("X-Forwarded-Host"),
            "forwarded_for": request.headers.get("X-Forwarded-For"),
            "accepts": request.headers.getlist("accept"),
        }

    @router.post("/echo")
    async def echo_body(payload: dict[str, object]) -> dict[str, object]:
        return {"received": payload}

    # Also mounted under the prefix, for the strip_prefix=False case: an
    # upstream that already namespaces its own routes.
    @router.get("/billing/echo")
    async def echo_prefixed(request: Request) -> dict[str, object]:
        return {"path": request.url.path}

    @router.get("/two-cookies")
    async def two_cookies() -> Response:
        response = Response(content=b"ok", media_type="text/plain")
        response.headers.append("set-cookie", "session=abc; Path=/; HttpOnly")
        response.headers.append("set-cookie", "csrf=xyz; Path=/")
        return response

    @router.get("/compressed")
    async def compressed() -> Response:
        return Response(
            content=gzip.compress(b'{"hello":"world"}'),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"},
        )

    return build_test_app(app_name="upstream", routers=[router])


def gateway_app(routes: list[dict[str, object]], **overrides):  # type: ignore[no-untyped-def]
    return build_test_app(
        plugins=["gateway"],
        app_name="gateway",
        raw={"plugin": {"gateway": {"routes": routes, "timeout": 2.0}}},
        **overrides,
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
    assert body["query"] == [["page", "2"]]


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
    # Port 1 with nothing on it. Linux refuses the connection and httpx raises
    # ConnectError; Windows lets it hang until it raises ConnectTimeout. Same
    # dead upstream, and it has to be 502 on both -- 504 says the upstream
    # answered slowly, which would send whoever is on call to the wrong place.
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


# -- what survives the hop ---------------------------------------------


async def test_a_repeated_query_parameter_survives_the_hop() -> None:
    # `?tag=a&tag=b` is two values. A dict() of the query keeps one of them,
    # and the upstream filters on half the request it was sent.
    upstream = upstream_app()
    gateway = gateway_app([{"prefix": "/billing", "target": "http://billing"}])

    async with client_for(upstream), client_for(gateway) as client:
        wire(gateway, upstream)
        response = await client.get("/billing/echo", params=[("tag", "a"), ("tag", "b")])

    assert response.json()["query"] == [["tag", "a"], ["tag", "b"]]


async def test_a_repeated_request_header_survives_the_hop() -> None:
    upstream = upstream_app()
    gateway = gateway_app([{"prefix": "/billing", "target": "http://billing"}])

    async with client_for(upstream), client_for(gateway) as client:
        wire(gateway, upstream)
        response = await client.get(
            "/billing/echo",
            headers=[("accept", "application/json"), ("accept", "text/plain")],
        )

    assert response.json()["accepts"] == ["application/json", "text/plain"]


async def test_two_set_cookie_headers_arrive_as_two() -> None:
    # Folding them into one comma-joined line is not a cosmetic loss: the
    # browser reads `session=abc` with `Path=/; HttpOnly, csrf=xyz; Path=/`
    # as its attributes, so the second cookie is never set and the first one
    # loses HttpOnly.
    upstream = upstream_app()
    gateway = gateway_app([{"prefix": "/billing", "target": "http://billing"}])

    async with client_for(upstream), client_for(gateway) as client:
        wire(gateway, upstream)
        response = await client.get("/billing/two-cookies")

    assert response.headers.get_list("set-cookie") == [
        "session=abc; Path=/; HttpOnly",
        "csrf=xyz; Path=/",
    ]


async def test_a_compressed_upstream_response_is_readable() -> None:
    # httpx decompresses before this code sees the body, so relaying
    # `Content-Encoding: gzip` labels plain bytes as gzip and every client
    # fails to decode them.
    upstream = upstream_app()
    gateway = gateway_app([{"prefix": "/billing", "target": "http://billing"}])

    async with client_for(upstream), client_for(gateway) as client:
        wire(gateway, upstream)
        response = await client.get("/billing/compressed")

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.json() == {"hello": "world"}
    assert response.headers["content-length"] == str(len(response.content))


async def test_a_client_cannot_forge_its_own_forwarded_address() -> None:
    # The upstream trusts the gateway as a proxy, so whatever reaches it in
    # X-Forwarded-For is an identity. Relaying the client's own claim makes
    # every allow-list behind the gateway settable by the caller.
    #
    # `trusted_proxies=[]` because the test client sits on loopback, which the
    # default list trusts: with the default this caller really is a proxy and
    # its chain is believable. Emptied, it is a direct client, and the only
    # honest answer is the address it connected from.
    upstream = upstream_app()
    gateway = gateway_app([{"prefix": "/billing", "target": "http://billing"}], trusted_proxies=[])

    async with client_for(upstream), client_for(gateway) as client:
        wire(gateway, upstream)
        response = await client.get("/billing/echo", headers={"X-Forwarded-For": "10.0.0.1"})

    assert response.json()["forwarded_for"] == "127.0.0.1"


async def test_the_resolved_client_address_reaches_the_upstream() -> None:
    # The other half: behind a proxy the framework does trust, the chain is
    # walked and the client it names is the one the upstream is told about.
    upstream = upstream_app()
    gateway = gateway_app(
        [{"prefix": "/billing", "target": "http://billing"}],
        trusted_proxies=["127.0.0.1/32"],
    )

    async with client_for(upstream), client_for(gateway) as client:
        wire(gateway, upstream)
        response = await client.get("/billing/echo", headers={"X-Forwarded-For": "198.51.100.7"})

    assert response.json()["forwarded_for"] == "198.51.100.7"


async def test_readiness_does_not_probe_upstreams() -> None:
    # A gateway whose upstream is restarting is still doing its job; cascading
    # readiness failures would take the whole system out together.
    gateway = gateway_app([{"prefix": "/billing", "target": "http://127.0.0.1:1"}])

    async with client_for(gateway) as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json()["checks"]["gateway"]["healthy"] is True
