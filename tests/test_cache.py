"""Cache: the promises a cache makes that a key-value store does not.

Three of them, and each is a way a service falls over without them: the
loader runs once per miss rather than once per caller, a dead Redis costs
latency rather than availability, and the hit rate is a number someone can
look at rather than a guess.

The backend is a fake because the interesting cases -- a Redis that raises
mid-request, five callers missing the same key at once -- are the ones a real
Redis will not perform on demand.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jfastframework.plugins.builtin.cache import Cache, CacheMetrics, CachePlugin
from jfastframework.testing import build_test_app, client_for


class FakeRedis:
    """The commands this cache issues, and nothing else."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        # Recorded rather than enforced: these tests care that a TTL was sent,
        # not that the fake expires anything.
        self.expiries: dict[str, int | None] = {}
        self.published: list[tuple[str, str]] = []
        self.calls: list[str] = []
        # Stands in for a Redis that is down, restarting, or failing over.
        self.broken = False

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if self.broken:
            raise ConnectionError("redis is down")

    async def get(self, key: str) -> str | None:
        self._guard("get")
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        self._guard("set")
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.expiries[key] = ex
        return True

    async def delete(self, *keys: str) -> int:
        self._guard("delete")
        return sum(self.values.pop(key, None) is not None for key in keys)

    async def exists(self, key: str) -> int:
        self._guard("exists")
        return int(key in self.values)

    async def publish(self, channel: str, message: str) -> None:
        self._guard("publish")
        self.published.append((channel, message))


def cache(client: FakeRedis, **overrides: Any) -> Cache:
    params: dict[str, Any] = {"prefix": "app:", "default_ttl": 60}
    params.update(overrides)
    return Cache(client, **params)


# -- round trip ---------------------------------------------------------


async def test_values_round_trip_through_json() -> None:
    client = FakeRedis()
    store = cache(client)
    await store.set("user:1", {"name": "Ada", "roles": ["admin"]})
    assert await store.get("user:1") == {"name": "Ada", "roles": ["admin"]}


async def test_keys_are_prefixed() -> None:
    client = FakeRedis()
    await cache(client).set("user:1", 1)
    assert list(client.values) == ["app:user:1"]


async def test_ttl_falls_back_to_the_default() -> None:
    client = FakeRedis()
    await cache(client).set("k", "v")
    assert client.expiries["app:k"] == 60


async def test_ttl_is_passed_through() -> None:
    client = FakeRedis()
    await cache(client).set("k", "v", ttl=5)
    assert client.expiries["app:k"] == 5


async def test_zero_ttl_stores_without_expiry() -> None:
    """The escape hatch: `ttl=None` means the default, so it cannot mean this."""
    client = FakeRedis()
    await cache(client).set("k", "v", ttl=0)
    assert client.expiries["app:k"] is None


async def test_a_negative_ttl_is_refused() -> None:
    # Redis rejects it too, but at the round trip. Failing here names the key.
    with pytest.raises(ValueError, match="ttl"):
        await cache(FakeRedis()).set("k", "v", ttl=-1)


async def test_get_returns_the_default_on_a_miss() -> None:
    assert await cache(FakeRedis()).get("nope", "fallback") == "fallback"


async def test_get_falls_back_to_the_raw_string() -> None:
    """Another system writes to this Redis, and it does not write JSON."""
    client = FakeRedis()
    client.values["app:laravel"] = "not json"
    assert await cache(client).get("laravel") == "not json"


async def test_a_cached_null_is_a_hit_not_a_miss() -> None:
    client = FakeRedis()
    store = cache(client)
    await store.set("empty", None)
    calls = 0

    async def loader() -> Any:
        nonlocal calls
        calls += 1
        return "recomputed"

    assert await store.get_or_set("empty", loader) is None
    assert calls == 0


async def test_delete_with_no_keys_touches_nothing() -> None:
    client = FakeRedis()
    assert await cache(client).delete() == 0
    assert client.calls == []


async def test_delete_prefixes_and_counts() -> None:
    client = FakeRedis()
    store = cache(client)
    await store.set("a", 1)
    await store.set("b", 2)
    assert await store.delete("a", "b", "missing") == 2
    assert client.values == {}


async def test_exists_is_prefixed() -> None:
    client = FakeRedis()
    store = cache(client)
    await store.set("a", 1)
    assert await store.exists("a") is True
    assert await store.exists("b") is False


async def test_publish_does_not_apply_the_key_prefix() -> None:
    """Channel names are a contract with whoever else is on this Redis.

    Prefixing them would silently rename the channel a Laravel publisher
    already agreed on. The `channels` plugin owns channel namespacing.
    """
    client = FakeRedis()
    await cache(client).publish("LARAVEL_CHEQUES_EVENTS", {"id": 7})
    assert client.published == [("LARAVEL_CHEQUES_EVENTS", '{"id": 7}')]


# -- get_or_set ---------------------------------------------------------


async def test_get_or_set_serves_a_hit_without_the_loader() -> None:
    client = FakeRedis()
    store = cache(client)
    await store.set("k", "cached")

    async def loader() -> str:
        raise AssertionError("loader must not run on a hit")

    assert await store.get_or_set("k", loader) == "cached"


async def test_get_or_set_stores_what_the_loader_returned() -> None:
    client = FakeRedis()
    store = cache(client)

    async def loader() -> dict[str, int]:
        return {"total": 42}

    assert await store.get_or_set("report", loader, ttl=30) == {"total": 42}
    assert client.expiries["app:report"] == 30
    assert await store.get("report") == {"total": 42}


async def test_get_or_set_survives_a_dead_backend() -> None:
    """This is what makes `health_critical=False` true instead of aspirational."""
    client = FakeRedis()
    client.broken = True

    async def loader() -> str:
        return "from the database"

    assert await cache(client).get_or_set("k", loader) == "from the database"


async def test_get_or_set_does_not_swallow_the_loader() -> None:
    # Degrading past a cache outage is the point; degrading past a broken
    # query is how a service serves wrong answers quietly.
    async def loader() -> str:
        raise RuntimeError("the query is broken")

    with pytest.raises(RuntimeError, match="the query is broken"):
        await cache(FakeRedis()).get_or_set("k", loader)


# -- stampede -----------------------------------------------------------


async def test_concurrent_misses_run_the_loader_once() -> None:
    client = FakeRedis()
    store = cache(client)
    calls = 0
    release = asyncio.Event()

    async def loader() -> int:
        nonlocal calls
        calls += 1
        await release.wait()
        return calls

    tasks = [asyncio.create_task(store.get_or_set("hot", loader)) for _ in range(5)]
    # Let every task reach the lock before the leader is allowed to finish.
    await asyncio.sleep(0.05)
    release.set()

    assert await asyncio.gather(*tasks) == [1, 1, 1, 1, 1]
    assert calls == 1


async def test_a_stalled_leader_does_not_stall_the_followers() -> None:
    """The bounded wait is the price of the lock, and it has to be bounded."""
    client = FakeRedis()
    store = cache(client, stampede_wait=0.05)
    release = asyncio.Event()

    async def stalled() -> str:
        await release.wait()
        return "from the leader"

    async def quick() -> str:
        return "from the follower"

    leader = asyncio.create_task(store.get_or_set("hot", stalled))
    await asyncio.sleep(0.01)
    # The leader is still inside its loader and holds the lock. The follower
    # must give up waiting and load for itself rather than queue behind it.
    assert await store.get_or_set("hot", quick) == "from the follower"

    release.set()
    assert await leader == "from the leader"


async def test_stampede_protection_can_be_turned_off() -> None:
    client = FakeRedis()
    store = cache(client, stampede_wait=0)

    async def loader() -> str:
        return "v"

    assert await store.get_or_set("k", loader) == "v"
    # No lock was taken: the only writes are the value itself.
    assert list(client.values) == ["app:k"]


# -- counters -----------------------------------------------------------


def registry() -> Any:
    from prometheus_client import CollectorRegistry

    return CollectorRegistry()


def sample(reg: Any, name: str) -> float:
    value = reg.get_sample_value(name)
    return 0.0 if value is None else float(value)


async def test_hits_and_misses_are_counted() -> None:
    reg = registry()
    client = FakeRedis()
    store = cache(client, metrics=CacheMetrics(reg))

    await store.get("cold")
    await store.set("warm", 1)
    await store.get("warm")

    assert sample(reg, "cache_misses_total") == 1.0
    assert sample(reg, "cache_hits_total") == 1.0


async def test_backend_errors_are_counted() -> None:
    reg = registry()
    client = FakeRedis()
    client.broken = True
    store = cache(client, metrics=CacheMetrics(reg))

    async def loader() -> str:
        return "v"

    await store.get_or_set("k", loader)
    assert sample(reg, "cache_errors_total") >= 1.0


async def test_suppressed_loads_are_counted() -> None:
    reg = registry()
    client = FakeRedis()
    store = cache(client, metrics=CacheMetrics(reg))
    release = asyncio.Event()

    async def loader() -> str:
        await release.wait()
        return "v"

    tasks = [asyncio.create_task(store.get_or_set("hot", loader)) for _ in range(3)]
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(*tasks)

    assert sample(reg, "cache_stampede_suppressed_total") == 2.0


async def test_counters_are_optional() -> None:
    """`metrics` is disableable, so the cache must work with no registry."""
    store = cache(FakeRedis(), metrics=CacheMetrics(None))
    await store.set("k", 1)
    assert await store.get("k") == 1


# -- the plugin ---------------------------------------------------------


def test_cache_is_ordered_after_metrics() -> None:
    # Soft ordering, not `requires`: a JSON service should not have to carry
    # prometheus-client to get a cache.
    assert "metrics" in CachePlugin.meta.after
    assert "metrics" not in CachePlugin.meta.requires


def test_cache_still_provides_the_same_keys() -> None:
    assert set(CachePlugin.meta.provides) == {"cache", "cache.client"}


async def test_cache_counters_reach_the_metrics_endpoint() -> None:
    app = build_test_app(
        plugins=["observability", "metrics", "cache"],
        extra_plugins=[CachePlugin],
    )
    async with client_for(app) as client:
        body = (await client.get("/metrics")).text
    assert "cache_hits_total" in body
    assert "cache_misses_total" in body
