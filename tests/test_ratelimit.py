"""Rate limiting: the atomicity, the response, and what happens when Redis is not there.

The double below models the one property Redis actually gives us: a script runs
to completion with nothing else interleaved. It yields to the event loop *before*
the critical section, so concurrent callers really do interleave -- and then
mutates its state without another await, which is what single-threaded execution
means. A limiter that reads and writes from Python instead of from a script races
under exactly that scheduling, and ``test_concurrent_requests_let_exactly_n_through``
is the test that catches it.

A real broker still belongs in CI. Set ``JFAST_TEST_REDIS_URL`` and the last test
in this file runs the same algorithm against it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Any

import pytest
from fastapi import APIRouter, Depends

from jfastframework.plugins.base import Plugin, PluginMeta
from jfastframework.plugins.builtin.ratelimit import (
    TOKEN_BUCKET_LUA,
    RateLimiter,
    RateLimitPlugin,
    RateLimitPolicy,
    rate_limit,
)
from jfastframework.testing import build_test_app, client_for


class FakeScript:
    """What ``client.register_script`` returns: one indivisible round trip."""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis

    async def __call__(self, keys: list[str], args: list[Any]) -> list[Any]:
        self._redis.calls.append("script")
        if self._redis.fail:
            raise ConnectionError("connection refused")
        # Yield here and only here. Everything after this point is what Redis
        # runs with the rest of the world stopped.
        await asyncio.sleep(0)
        return self._redis.token_bucket(keys[0], *(float(a) for a in args))


class FakeRedis:
    """Just the commands the limiter is allowed to issue, plus the ones it is not."""

    def __init__(self) -> None:
        self.buckets: dict[str, tuple[float, float]] = {}
        self.calls: list[str] = []
        self.fail = False
        self.now = 1_700_000_000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def register_script(self, script: str) -> FakeScript:
        assert script == TOKEN_BUCKET_LUA
        return FakeScript(self)

    async def ping(self) -> bool:
        if self.fail:
            raise ConnectionError("connection refused")
        return True

    def token_bucket(
        self, key: str, burst: float, rate: float, cost: float, ttl: float
    ) -> list[Any]:
        tokens, ts = self.buckets.get(key, (burst, self.now))
        tokens = min(burst, tokens + max(0.0, self.now - ts) * rate)
        allowed = 0
        if tokens >= cost:
            allowed = 1
            tokens -= cost
        self.buckets[key] = (tokens, self.now)
        retry = 0.0 if allowed else (cost - tokens) / rate
        return [allowed, str(tokens), str(retry), str((burst - tokens) / rate)]

    # -- commands a correct limiter never issues ------------------------

    async def hmget(self, key: str, *fields: str) -> list[Any]:
        self.calls.append("hmget")
        await asyncio.sleep(0)
        tokens, ts = self.buckets.get(key, (None, None))  # type: ignore[assignment]
        return [tokens, ts]

    async def hset(self, key: str, mapping: dict[str, Any]) -> int:
        self.calls.append("hset")
        await asyncio.sleep(0)
        self.buckets[key] = (float(mapping["tokens"]), float(mapping["ts"]))
        return 1


class FakeCachePlugin(Plugin):
    """Stands in for the cache plugin, publishing the one key ratelimit needs."""

    meta = PluginMeta(name="cache", provides=("cache.client",), health_critical=False)
    client = FakeRedis()

    def register(self, ctx: Any) -> None:
        ctx.provide("cache.client", type(self).client)


def build_app(
    *,
    redis: FakeRedis,
    routes: APIRouter | None = None,
    **ratelimit_config: Any,
) -> Any:
    FakeCachePlugin.client = redis
    router = routes if routes is not None else APIRouter()
    if routes is None:

        @router.get("/things")
        async def things() -> dict[str, str]:
            return {"ok": "yes"}

    return build_test_app(
        plugins=["cache", "ratelimit"],
        extra_plugins=[FakeCachePlugin, RateLimitPlugin],
        routers=[router],
        raw={"plugin": {"ratelimit": ratelimit_config}},
    )


# -- atomicity ----------------------------------------------------------


async def test_check_is_a_single_script_call() -> None:
    """No read-then-write from Python: one round trip, and it is the script."""
    redis = FakeRedis()
    limiter = RateLimiter(redis, prefix="t:")
    policy = RateLimitPolicy(limit=5, window=60.0)

    await limiter.check(policy, "ip:1.2.3.4")
    await limiter.check(policy, "ip:1.2.3.4")

    assert redis.calls == ["script", "script"]


async def test_concurrent_requests_let_exactly_n_through() -> None:
    """20 requests at once against a limit of 5. Exactly 5 pass."""
    redis = FakeRedis()
    app = build_app(redis=redis, limit=5, window=60.0)

    async with client_for(app) as client:
        responses = await asyncio.gather(*(client.get("/things") for _ in range(20)))

    codes = [r.status_code for r in responses]
    assert codes.count(200) == 5, f"expected exactly 5 allowed, got {codes.count(200)}"
    assert codes.count(429) == 15


async def test_tokens_refill_over_time() -> None:
    redis = FakeRedis()
    limiter = RateLimiter(redis, prefix="t:")
    policy = RateLimitPolicy(limit=6, window=60.0)

    for _ in range(6):
        assert (await limiter.check(policy, "sub:ana")).allowed
    assert not (await limiter.check(policy, "sub:ana")).allowed

    redis.advance(30.0)  # half a window -> three tokens back
    assert (await limiter.check(policy, "sub:ana")).allowed
    assert (await limiter.check(policy, "sub:ana")).allowed
    assert (await limiter.check(policy, "sub:ana")).allowed
    assert not (await limiter.check(policy, "sub:ana")).allowed


# -- the response -------------------------------------------------------


async def test_429_body_is_a_problem_document() -> None:
    redis = FakeRedis()
    app = build_app(redis=redis, limit=1, window=60.0)

    async with client_for(app) as client:
        assert (await client.get("/things")).status_code == 200
        refused = await client.get("/things")

    assert refused.status_code == 429
    assert refused.headers["content-type"] == "application/problem+json"
    body = refused.json()
    assert body["status"] == 429
    assert body["title"] == "Too Many Requests"
    assert body["type"] == "about:blank"
    assert body["instance"] == "/things"
    assert body["detail"]
    assert body["limit"] == 1


async def test_headers_on_allow_and_on_refusal() -> None:
    redis = FakeRedis()
    app = build_app(redis=redis, limit=3, window=60.0)

    async with client_for(app) as client:
        allowed = await client.get("/things")
        assert allowed.headers["RateLimit-Limit"] == "3"
        assert allowed.headers["RateLimit-Remaining"] == "2"
        assert int(allowed.headers["RateLimit-Reset"]) == 20  # one token per 20s

        await client.get("/things")
        await client.get("/things")
        refused = await client.get("/things")

    assert refused.status_code == 429
    assert refused.headers["RateLimit-Limit"] == "3"
    assert refused.headers["RateLimit-Remaining"] == "0"
    assert int(refused.headers["Retry-After"]) >= 1
    assert int(refused.headers["RateLimit-Reset"]) >= int(refused.headers["Retry-After"])


async def test_probes_are_exempt() -> None:
    redis = FakeRedis()
    app = build_app(redis=redis, limit=1, window=60.0)

    async with client_for(app) as client:
        for _ in range(5):
            assert (await client.get("/health")).status_code == 200


async def test_routes_added_straight_to_the_app_are_limited() -> None:
    """What the gateway plugin does: ``add_api_route``, not ``include_router``."""
    redis = FakeRedis()
    app = build_app(redis=redis, limit=1, window=60.0)

    async def proxy(path: str = "") -> dict[str, str]:
        return {"ok": "yes"}

    app.add_api_route("/upstream/{path:path}", proxy, methods=["GET"])

    async with client_for(app) as client:
        assert (await client.get("/upstream/a")).status_code == 200
        assert (await client.get("/upstream/b")).status_code == 429


# -- outage -------------------------------------------------------------


async def test_backend_outage_fails_open_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    redis = FakeRedis()
    redis.fail = True
    app = build_app(redis=redis, limit=1, window=60.0)

    with caplog.at_level(logging.WARNING, logger="jfast.ratelimit"):
        async with client_for(app) as client:
            first = await client.get("/things")
            second = await client.get("/things")

    assert first.status_code == 200
    assert second.status_code == 200, "a cache outage must not become a total outage"
    assert any("failing open" in record.getMessage().lower() for record in caplog.records)


async def test_outage_shows_up_in_ready() -> None:
    redis = FakeRedis()
    redis.fail = True
    app = build_app(redis=redis, limit=1, window=60.0)

    async with client_for(app) as client:
        await client.get("/things")
        ready = await client.get("/ready")

    assert ready.status_code == 200, "a limiter failing open does not take the pod out"
    body = ready.json()
    assert body["status"] == "degraded"
    check = body["checks"]["ratelimit"]
    assert check["healthy"] is False
    assert check["critical"] is False


# -- per-route override -------------------------------------------------


async def test_per_route_override_overrides() -> None:
    router = APIRouter()

    @router.get("/cheap")
    async def cheap() -> dict[str, str]:
        return {"ok": "yes"}

    @router.get("/search", dependencies=[Depends(rate_limit(limit=2, window=60.0))])
    async def search() -> dict[str, str]:
        return {"ok": "yes"}

    redis = FakeRedis()
    app = build_app(redis=redis, routes=router, limit=10, window=60.0)

    async with client_for(app) as client:
        codes = [(await client.get("/search")).status_code for _ in range(4)]
        assert codes == [200, 200, 429, 429], "the route limit of 2 must win over the default of 10"

        # The override is its own bucket: the default limit is untouched.
        for _ in range(10):
            assert (await client.get("/cheap")).status_code == 200
        assert (await client.get("/cheap")).status_code == 429


async def test_override_route_is_not_double_counted() -> None:
    router = APIRouter()

    @router.get("/search", dependencies=[Depends(rate_limit(limit=4, window=60.0))])
    async def search() -> dict[str, str]:
        return {"ok": "yes"}

    redis = FakeRedis()
    app = build_app(redis=redis, routes=router, limit=100, window=60.0)

    async with client_for(app) as client:
        first = await client.get("/search")
        assert first.headers["RateLimit-Limit"] == "4"
        assert first.headers["RateLimit-Remaining"] == "3", "the default must not also charge it"


async def test_cost_multiplies_the_charge() -> None:
    router = APIRouter()

    @router.get("/vectors", dependencies=[Depends(rate_limit(limit=10, window=60.0, cost=5))])
    async def vectors() -> dict[str, str]:
        return {"ok": "yes"}

    redis = FakeRedis()
    app = build_app(redis=redis, routes=router, limit=100, window=60.0)

    async with client_for(app) as client:
        assert (await client.get("/vectors")).status_code == 200
        assert (await client.get("/vectors")).status_code == 200
        assert (await client.get("/vectors")).status_code == 429


# -- identity -----------------------------------------------------------


async def test_principal_beats_ip() -> None:
    """Two users behind one NAT get one bucket each, not one between them."""
    from jfastframework.auth.principal import Principal

    router = APIRouter()

    @router.get("/things")
    async def things() -> dict[str, str]:
        return {"ok": "yes"}

    redis = FakeRedis()
    app = build_app(redis=redis, routes=router, limit=1, window=60.0)

    class Impersonate:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                subject = dict(scope["headers"]).get(b"x-test-subject", b"anon").decode()
                scope.setdefault("state", {})["principal"] = Principal(subject=subject)
            await self.inner(scope, receive, send)

    wrapped = Impersonate(app)

    import httpx

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wrapped), base_url="http://test"
        ) as client,
        app.router.lifespan_context(app),
    ):
        assert (await client.get("/things", headers={"X-Test-Subject": "ana"})).status_code == 200
        assert (await client.get("/things", headers={"X-Test-Subject": "beto"})).status_code == 200
        assert (await client.get("/things", headers={"X-Test-Subject": "ana"})).status_code == 429

    assert any(key.endswith("sub:ana") for key in redis.buckets)


async def test_client_ip_comes_from_the_proxy_resolver_when_present() -> None:
    """``request.state.client_ip`` is the contract with the trusted-proxy work."""
    redis = FakeRedis()
    app = build_app(redis=redis, limit=1, window=60.0)

    class Resolve:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                ip = dict(scope["headers"]).get(b"x-test-ip", b"0.0.0.0").decode()
                scope.setdefault("state", {})["client_ip"] = ip
            await self.inner(scope, receive, send)

    import httpx

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=Resolve(app)), base_url="http://test"
        ) as client,
        app.router.lifespan_context(app),
    ):
        assert (await client.get("/things", headers={"X-Test-IP": "10.0.0.1"})).status_code == 200
        assert (await client.get("/things", headers={"X-Test-IP": "10.0.0.2"})).status_code == 200
        assert (await client.get("/things", headers={"X-Test-IP": "10.0.0.1"})).status_code == 429

    assert any(key.endswith("ip:10.0.0.1") for key in redis.buckets)


# -- against a real broker ----------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("JFAST_TEST_REDIS_URL"),
    reason="set JFAST_TEST_REDIS_URL to run the Lua script against a real Redis",
)
async def test_lua_script_against_real_redis() -> None:
    import uuid

    import redis.asyncio as aioredis

    client = aioredis.from_url(os.environ["JFAST_TEST_REDIS_URL"], decode_responses=True)
    limiter = RateLimiter(client, prefix=f"jfast-test-{uuid.uuid4().hex}:")
    policy = RateLimitPolicy(limit=5, window=60.0)
    try:
        decisions = await asyncio.gather(*(limiter.check(policy, "sub:ana") for _ in range(20)))
        assert sum(d.allowed for d in decisions) == 5
        refused = next(d for d in decisions if not d.allowed)
        assert refused.retry_after >= 1
        assert refused.reset >= refused.retry_after
        assert math.isclose(refused.remaining, 0, abs_tol=1)
    finally:
        await client.aclose()
