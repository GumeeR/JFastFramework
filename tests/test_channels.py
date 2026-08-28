"""Declared channels, and the placement rule that keeps modules apart.

Both exist for the same reason: coupling that is easy to add, invisible once
added, and expensive to remove after a second person has copied the pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jfastframework.channels import (
    Channel,
    ChannelError,
    ChannelRegistry,
    MemoryBackend,
    clear_pending,
    pending_subscriptions,
)
from jfastframework.contracts import Contract, check_placement


@pytest.fixture(autouse=True)
def _clean() -> None:
    clear_pending()


# -- payloads -----------------------------------------------------------


async def test_a_payload_is_validated_where_it_is_built() -> None:
    """Not in the subscriber, three services away from the change."""
    channel = Channel("orders.placed", required=("order_id", "rfc"))
    channel.bind(MemoryBackend())

    with pytest.raises(ChannelError, match="missing rfc"):
        await channel.publish({"order_id": 7})


async def test_a_complete_payload_goes_through() -> None:
    backend = MemoryBackend()
    channel = Channel("orders.placed", required=("order_id",))
    channel.bind(backend)

    await channel.publish({"order_id": 7})
    assert backend.published == [("orders.placed", {"order_id": 7})]


async def test_publishing_without_a_backend_says_what_to_enable() -> None:
    channel = Channel("orphan")
    with pytest.raises(ChannelError, match="no backend"):
        await channel.publish({})


# -- delivery -----------------------------------------------------------


async def test_every_subscriber_receives_it() -> None:
    backend = MemoryBackend()
    channel = Channel("thing.happened")
    channel.bind(backend)
    seen: list[str] = []

    async def first(payload: dict) -> None:
        seen.append("first")

    async def second(payload: dict) -> None:
        seen.append("second")

    await channel.subscribe(first)
    await channel.subscribe(second)
    await channel.publish({})

    assert sorted(seen) == ["first", "second"]


async def test_one_failing_handler_does_not_stop_the_others() -> None:
    """A subscriber with a bug must not silently disable its neighbours."""
    backend = MemoryBackend()
    channel = Channel("thing.happened")
    channel.bind(backend)
    survived: list[str] = []

    async def broken(payload: dict) -> None:
        raise RuntimeError("boom")

    async def healthy(payload: dict) -> None:
        survived.append("ran")

    await channel.subscribe(broken)
    await channel.subscribe(healthy)
    await channel.publish({})

    assert survived == ["ran"]


def test_the_decorator_defers_registration_to_startup() -> None:
    """Importing a module must not require a running event loop."""
    channel = Channel("late")

    @channel.on
    async def handle(payload: dict) -> None: ...

    assert [c.name for c, _ in pending_subscriptions()] == ["late"]


# -- the registry -------------------------------------------------------


def test_two_channels_cannot_share_a_name() -> None:
    """A channel name is a wire contract; two is a message going somewhere else."""
    registry = ChannelRegistry()
    registry.register(Channel("shared.name"))
    with pytest.raises(ChannelError, match="both called"):
        registry.register(Channel("shared.name"))


def test_the_registry_can_list_what_a_service_talks_about() -> None:
    registry = ChannelRegistry()
    registry.register(Channel("b.happened", backend="redis", description="second"))
    registry.register(Channel("a.happened", required=("id",)))

    described = registry.describe()
    assert [d["name"] for d in described] == ["a.happened", "b.happened"]
    assert described[1]["backend"] == "redis"
    assert described[0]["required"] == ["id"]


def test_backends_are_per_channel() -> None:
    """Mixing is the normal case: one channel Laravel reads, one internal."""
    internal = Channel("internal.thing")
    crossing = Channel("LARAVEL_CHEQUES_EVENTS", backend="redis")
    assert internal.backend == "memory"
    assert crossing.backend == "redis"


# -- placement ----------------------------------------------------------


def _service(tmp_path: Path, files: dict[str, str]) -> Path:
    for relative, body in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def test_a_module_importing_another_is_reported_with_where_to_move_it(
    tmp_path: Path,
) -> None:
    root = _service(
        tmp_path,
        {
            "modules/invoice/enums.py": "from enum import Enum\n",
            "modules/payment/service.py": "from modules.invoice.enums import Status\n",
        },
    )
    found = check_placement(Contract(project="t"), root)

    assert len(found) == 1
    assert found[0].rule == "cross-module"
    assert "shared/enums.py" in found[0].why


def test_shared_may_not_import_a_module(tmp_path: Path) -> None:
    root = _service(
        tmp_path,
        {
            "modules/invoice/enums.py": "from enum import Enum\n",
            "shared/enums.py": "from modules.invoice.enums import Status\n",
        },
    )
    found = check_placement(Contract(project="t"), root)

    assert len(found) == 1
    assert found[0].rule == "shared-direction"
    assert "one-way" in found[0].why


def test_a_module_importing_shared_is_fine(tmp_path: Path) -> None:
    """The direction that is allowed, and the whole point of the layer."""
    root = _service(
        tmp_path,
        {
            "shared/enums.py": "from enum import Enum\n",
            "modules/payment/service.py": "from shared.enums import Currency\n",
        },
    )
    assert check_placement(Contract(project="t"), root) == []


def test_a_module_importing_itself_is_fine(tmp_path: Path) -> None:
    root = _service(
        tmp_path,
        {
            "modules/payment/enums.py": "from enum import Enum\n",
            "modules/payment/service.py": "from modules.payment.enums import Kind\n",
        },
    )
    assert check_placement(Contract(project="t"), root) == []


def test_a_waiver_clears_it(tmp_path: Path) -> None:
    root = _service(
        tmp_path,
        {
            "modules/invoice/enums.py": "from enum import Enum\n",
            "modules/payment/service.py": (
                "from modules.invoice.enums import Status  # contracts: allow migrating\n"
            ),
        },
    )
    assert check_placement(Contract(project="t"), root) == []


def test_the_rule_can_be_turned_off(tmp_path: Path) -> None:
    root = _service(
        tmp_path,
        {"modules/payment/service.py": "from modules.invoice.enums import Status\n"},
    )
    contract = Contract(project="t", enforce_placement=False)
    assert check_placement(contract, root) == []
