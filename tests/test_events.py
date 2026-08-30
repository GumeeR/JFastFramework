"""Event streaming: handler registration, and the broker container it needs.

There is no broker in CI, so nothing here talks to Kafka. What is tested is the
part that fails without one: whether a declared handler is actually known to
the consumer before it joins its group, and whether the generated compose file
describes a broker a client can reach.
"""

from __future__ import annotations

from fastapi import FastAPI

from jfastframework.context import AppContext
from jfastframework.deploy import build_compose
from jfastframework.plugins.builtin import events
from jfastframework.testing import make_config


def _context(**overrides: object) -> AppContext:
    return AppContext(app=FastAPI(), config=make_config(**overrides))


# -- registration -------------------------------------------------------


async def test_a_handler_declared_at_import_time_reaches_the_bus() -> None:
    """The bus does not exist until ``register`` runs, so a decorator that
    binds straight to it has a registration window one line wide. Declaring at
    import time and binding later is the only order a module can rely on."""
    seen: list[str] = []

    @events.on("orders")
    async def handle(event: events.Event) -> None:
        seen.append(event.type)

    try:
        plugin = events.EventsPlugin()
        ctx = _context()
        plugin.register(ctx)

        bus = ctx.require("events", events.EventBus)
        assert bus.topics == ("orders",)

        await bus.dispatch("orders", events.Event(type="order.paid"))
        assert seen == ["order.paid"]
    finally:
        events.clear_pending()


async def test_a_declared_topic_is_prefixed_like_a_published_one() -> None:
    async def handle(event: events.Event) -> None: ...

    events.on("orders")(handle)
    try:
        plugin = events.EventsPlugin({"topic_prefix": "billing."})
        ctx = _context()
        plugin.register(ctx)

        assert ctx.require("events", events.EventBus).topics == ("billing.orders",)
    finally:
        events.clear_pending()


async def test_binding_the_same_handler_twice_delivers_one_copy() -> None:
    """The pending list is drained at register and again at startup."""
    seen: list[str] = []

    async def handle(event: events.Event) -> None:
        seen.append(event.id)

    bus = events.EventBus(None, source="test", topic_prefix="")
    bus.subscribe("orders", handle)
    bus.subscribe("orders", handle)

    await bus.dispatch("orders", events.Event(type="order.paid", id="e1"))
    assert seen == ["e1"]


# -- infrastructure -----------------------------------------------------


def test_the_broker_image_has_a_resolvable_default_and_is_overridable() -> None:
    """`bitnami/kafka:3.9` stopped resolving when Bitnami moved its catalogue."""
    assert events.EventsPlugin().infra()[0].image == "bitnamilegacy/kafka:3.9"

    pinned = events.EventsPlugin({"image": "apache/kafka:3.9.0"})
    assert pinned.infra()[0].image == "apache/kafka:3.9.0"


def test_the_broker_advertises_an_address_reachable_from_the_host() -> None:
    """`jfast dev` runs the API on the host, where `kafka:9092` does not
    resolve. A client that bootstraps and then reconnects to an unroutable
    advertised address appears to connect, then hangs."""
    compose = build_compose(make_config(port=8000), [events.EventsPlugin()])
    kafka = compose["services"]["kafka"]

    assert kafka["ports"] == ["8002:9094"]
    advertised = kafka["environment"]["KAFKA_CFG_ADVERTISED_LISTENERS"]
    assert "INTERNAL://kafka:9092" in advertised
    assert "EXTERNAL://localhost:8002" in advertised


def test_both_advertised_listeners_are_declared_and_mapped() -> None:
    infra = events.EventsPlugin().infra()[0]
    environment = infra.environment

    assert environment["KAFKA_CFG_LISTENERS"] == (
        "INTERNAL://:9092,CONTROLLER://:9093,EXTERNAL://:9094"
    )
    assert environment["KAFKA_CFG_INTER_BROKER_LISTENER_NAME"] == "INTERNAL"
    for name in ("CONTROLLER", "INTERNAL", "EXTERNAL"):
        assert f"{name}:PLAINTEXT" in environment["KAFKA_CFG_LISTENER_SECURITY_PROTOCOL_MAP"]


def test_host_port_moves_the_published_port_and_the_advertised_one_together() -> None:
    """The escape hatch for a base port `infra()` cannot see: `collect_infra`
    calls it without a context.

    Both halves, because moving one without the other is the exact failure this
    setting exists to prevent -- `ports: - "8702:9094"` against
    `EXTERNAL://localhost:19092` is a client that bootstraps, reconnects to the
    advertised address and hangs.
    """
    compose = build_compose(make_config(port=8700), [events.EventsPlugin({"host_port": 19092})])
    kafka = compose["services"]["kafka"]

    assert kafka["ports"] == ["19092:9094"]
    assert "EXTERNAL://localhost:19092" in kafka["environment"]["KAFKA_CFG_ADVERTISED_LISTENERS"]


def test_infra_can_be_turned_off() -> None:
    assert events.EventsPlugin({"include_infra": False}).infra() == []
