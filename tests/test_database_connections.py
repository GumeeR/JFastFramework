"""Named database instances, and the single-connection case that must survive.

``RESOURCE_TYPES`` names four datastore *types*; the database plugin held one
DSN. A read replica, a per-tenant database and a shard are not four features,
they are four views of the same missing structure: a database the service can
name. These tests describe that structure, and pin the unnamed configuration
so every existing project keeps working untouched.
"""

from __future__ import annotations

from typing import Any

import pytest

from jfastframework.errors import PluginError
from jfastframework.plugins.builtin.database import (
    DEFAULT_CONNECTION,
    DatabasePlugin,
)
from jfastframework.testing import build_test_app

PRIMARY_DSN = "postgresql+asyncpg://app:pw@primary:5432/app"
REPLICA_DSN = "postgresql+asyncpg://app:pw@replica:5432/app"


def _register(config: dict[str, Any] | None = None) -> tuple[DatabasePlugin, Any]:
    app = build_test_app()
    ctx = app.state.jfast
    plugin = DatabasePlugin(config or {})
    plugin.register(ctx)
    return plugin, ctx


# -- the unnamed case, which must not move ------------------------------


def test_one_dsn_still_provides_one_engine() -> None:
    """Every generated template and every existing project does exactly this."""
    _, ctx = _register({"dsn": PRIMARY_DSN})

    assert ctx.require("db.engine") is not None
    assert ctx.require("db.sessionmaker") is not None


def test_the_unnamed_connection_is_the_default_instance() -> None:
    plugin, ctx = _register({"dsn": PRIMARY_DSN})
    databases = ctx.require("db.databases")

    assert databases.names == (DEFAULT_CONNECTION,)
    assert databases.engine(DEFAULT_CONNECTION) is ctx.require("db.engine")
    assert plugin.settings.pool_size == 10


# -- named instances -----------------------------------------------------


def test_two_named_connections_get_two_engines() -> None:
    _, ctx = _register(
        {
            "connections": {
                "primary": {"dsn": PRIMARY_DSN},
                "replica": {"dsn": REPLICA_DSN, "read_only": True},
            }
        }
    )
    databases = ctx.require("db.databases")

    assert databases.names == ("primary", "replica")
    assert databases.engine("primary") is not databases.engine("replica")
    assert str(databases.engine("replica").url).endswith("@replica:5432/app")


def test_the_writable_connection_is_what_db_engine_means() -> None:
    """`ctx.require("db.engine")` predates instances; it keeps meaning "write"."""
    _, ctx = _register(
        {
            "connections": {
                "primary": {"dsn": PRIMARY_DSN},
                "replica": {"dsn": REPLICA_DSN, "read_only": True},
            }
        }
    )
    databases = ctx.require("db.databases")

    assert databases.default_name == "primary"
    assert ctx.require("db.engine") is databases.engine("primary")
    assert databases.replica_names == ("replica",)


def test_a_connection_reads_its_own_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JFAST_DB_DSN", PRIMARY_DSN)
    monkeypatch.setenv("JFAST_DB_REPLICA_DSN", REPLICA_DSN)
    _, ctx = _register(
        {
            "connections": {
                "primary": {"dsn_env": "JFAST_DB_DSN"},
                "replica": {"dsn_env": "JFAST_DB_REPLICA_DSN", "read_only": True},
            }
        }
    )
    databases = ctx.require("db.databases")

    assert str(databases.engine("primary").url).endswith("@primary:5432/app")
    assert str(databases.engine("replica").url).endswith("@replica:5432/app")


def test_a_connection_named_replica_defaults_to_its_own_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`JFAST_DB_<NAME>_DSN`, so a second instance needs no `dsn_env` line."""
    monkeypatch.setenv("JFAST_DB_DSN", PRIMARY_DSN)
    monkeypatch.setenv("JFAST_DB_ANALYTICS_DSN", REPLICA_DSN)
    _, ctx = _register({"connections": {"default": {}, "analytics": {"read_only": True}}})
    databases = ctx.require("db.databases")

    assert str(databases.engine("analytics").url).endswith("@replica:5432/app")


def test_a_missing_dsn_names_the_variable_that_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JFAST_DB_REPLICA_DSN", raising=False)
    with pytest.raises(PluginError, match="JFAST_DB_REPLICA_DSN"):
        _register(
            {
                "connections": {
                    "primary": {"dsn": PRIMARY_DSN},
                    "replica": {"read_only": True},
                }
            }
        )


def test_a_service_with_only_read_only_connections_is_refused() -> None:
    """Nothing could write. Fail at boot, not at the first POST."""
    with pytest.raises(PluginError, match="read_only"):
        _register({"connections": {"replica": {"dsn": REPLICA_DSN, "read_only": True}}})


# -- pools are per instance ----------------------------------------------


def test_pool_size_is_per_connection_and_falls_back_to_the_global_one() -> None:
    _, ctx = _register(
        {
            "pool_size": 12,
            "connections": {
                "primary": {"dsn": PRIMARY_DSN},
                "replica": {"dsn": REPLICA_DSN, "read_only": True, "pool_size": 3},
            },
        }
    )
    databases = ctx.require("db.databases")

    assert databases.engine("primary").pool.size() == 12
    assert databases.engine("replica").pool.size() == 3


# -- what `jfast describe --json` shows ----------------------------------


def test_describe_lists_the_instances_without_leaking_a_dsn() -> None:
    plugin, _ = _register(
        {
            "connections": {
                "primary": {"dsn": PRIMARY_DSN},
                "replica": {"dsn": REPLICA_DSN, "read_only": True, "pool_size": 3},
            }
        }
    )
    described = plugin.describe()
    connections = {entry["name"]: entry for entry in described["connections"]}

    assert set(connections) == {"primary", "replica"}
    assert connections["primary"]["role"] == "primary"
    assert connections["replica"]["role"] == "replica"
    assert connections["replica"]["pool_size"] == 3
    assert "pw@" not in repr(described)


def test_describe_states_the_connection_ceiling() -> None:
    """The number that decides whether PostgreSQL survives the deployment."""
    plugin, _ = _register(
        {
            "pool_size": 10,
            "max_overflow": 20,
            "connections": {
                "primary": {"dsn": PRIMARY_DSN},
                "replica": {"dsn": REPLICA_DSN, "read_only": True},
            },
        }
    )
    described = plugin.describe()

    # Two instances, each 10 + 20 per process.
    assert described["max_connections"] == 60


# -- containers ----------------------------------------------------------


def test_one_connection_declares_exactly_the_container_it_always_did() -> None:
    plugin = DatabasePlugin({"dsn": PRIMARY_DSN})
    (infra,) = plugin.infra()

    assert infra.name == "postgres"
    assert infra.port_offset == 1
    assert infra.volumes == ["postgres_data:/var/lib/postgresql/data"]


def test_each_connection_declares_its_own_container() -> None:
    plugin = DatabasePlugin(
        {
            "connections": {
                "primary": {"dsn": PRIMARY_DSN},
                "replica": {"dsn": REPLICA_DSN, "read_only": True},
            }
        }
    )
    declared = {service.name: service for service in plugin.infra()}

    assert set(declared) == {"postgres", "postgres-replica"}
    offsets = {service.port_offset for service in declared.values()}
    assert len(offsets) == 2
    assert declared["postgres-replica"].volumes == [
        "postgres_replica_data:/var/lib/postgresql/data"
    ]


def test_a_connection_can_opt_out_of_its_container() -> None:
    """A managed replica has no container to generate."""
    plugin = DatabasePlugin(
        {
            "connections": {
                "primary": {"dsn": PRIMARY_DSN},
                "replica": {"dsn": REPLICA_DSN, "read_only": True, "include_infra": False},
            }
        }
    )
    assert [service.name for service in plugin.infra()] == ["postgres"]


def test_more_containers_than_the_port_block_holds_is_refused() -> None:
    """Ten ports per service, and the other plugins already claim some."""
    connections = {
        "primary": {"dsn": PRIMARY_DSN},
        "a": {"dsn": REPLICA_DSN},
        "b": {"dsn": REPLICA_DSN},
        "c": {"dsn": REPLICA_DSN},
        "d": {"dsn": REPLICA_DSN},
    }
    plugin = DatabasePlugin({"connections": connections})
    with pytest.raises(ValueError, match="port block"):
        plugin.infra()
