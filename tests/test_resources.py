"""The resource graph: named instances, bindings, and what derives from them.

The bug this replaces: the workspace recorded datastores by *type*, so the
generated compose file created a container and nothing wrote the DSN that
pointed at it. A second database of the same type could not be expressed at
all. Both are asserted here, along with the compatibility that lets a 0.1
workspace file keep producing exactly what it produced before.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from jfastframework.deploy.workspace import build_workspace_compose
from jfastframework.graph import render_graph
from jfastframework.resources import Binding, Resource
from jfastframework.workspace import ServiceEntry, Workspace


def _workspace() -> Workspace:
    return Workspace(name="cometax", base_port=8000)


def _service(name: str, port: int, **kwargs: object) -> ServiceEntry:
    return ServiceEntry(name=name, kind="api", port=port, path=name, **kwargs)  # type: ignore[arg-type]


# -- what could not be said before -------------------------------------


def test_two_databases_of_the_same_type() -> None:
    workspace = _workspace()
    workspace.add(_service("billing", 8010))
    workspace.add_resource(Resource(name="core-db", type="postgres", port=8900))
    workspace.add_resource(Resource(name="analytics-db", type="postgres", port=8901))

    workspace.link("billing", "core-db")
    workspace.link("billing", "analytics-db", env="JFAST_ANALYTICS_DSN")

    env = workspace.environment_for(workspace.services[0])
    assert "core-db" in env["JFAST_DB_DSN"]
    assert "analytics-db" in env["JFAST_ANALYTICS_DSN"]


def test_one_resource_shared_by_two_services() -> None:
    """Per-service containers made sharing impossible; per-resource does not."""
    workspace = _workspace()
    workspace.add(_service("billing", 8010))
    workspace.add(_service("catalog", 8020))
    workspace.add_resource(Resource(name="shared-redis", type="redis", port=8900))
    workspace.link("billing", "shared-redis")
    workspace.link("catalog", "shared-redis")

    compose = build_workspace_compose(workspace, with_caddy=False)
    redis_containers = [name for name in compose["services"] if "redis" in name]
    assert redis_containers == ["shared-redis"]
    assert compose["services"]["billing"]["environment"]["JFAST_CACHE_URL"] == (
        "redis://shared-redis:6379/0"
    )
    assert compose["services"]["catalog"]["environment"]["JFAST_CACHE_URL"] == (
        "redis://shared-redis:6379/0"
    )


def test_the_dsn_is_written_beside_the_container() -> None:
    """The gap this whole step exists to close."""
    workspace = _workspace()
    workspace.add(_service("billing", 8010))
    workspace.add_resource(Resource(name="core-db", type="postgres", port=8900))
    workspace.link("billing", "core-db")

    compose = build_workspace_compose(workspace, with_caddy=False)
    service = compose["services"]["billing"]
    assert "core-db" in compose["services"]
    assert service["environment"]["JFAST_DB_DSN"].startswith("postgresql+asyncpg://app:")
    assert "@core-db:5432/app" in service["environment"]["JFAST_DB_DSN"]
    assert service["depends_on"]["core-db"]["condition"] == "service_healthy"


def test_each_resource_has_its_own_secret() -> None:
    """One shared POSTGRES_PASSWORD made a leak anywhere a leak everywhere."""
    workspace = _workspace()
    workspace.add_resource(Resource(name="core-db", type="postgres", port=8900))
    workspace.add_resource(Resource(name="analytics-db", type="postgres", port=8901))

    compose = build_workspace_compose(workspace, with_caddy=False)
    core = compose["services"]["core-db"]["environment"]["POSTGRES_PASSWORD"]
    analytics = compose["services"]["analytics-db"]["environment"]["POSTGRES_PASSWORD"]
    assert "CORE_DB_PASSWORD" in core
    assert "ANALYTICS_DB_PASSWORD" in analytics


# -- compatibility with the 0.1 file -----------------------------------


def test_a_legacy_workspace_still_generates_what_it_did() -> None:
    workspace = _workspace()
    workspace.add(_service("billing", 8010, datastores=["database", "cache"]))

    compose = build_workspace_compose(workspace, with_caddy=False)
    assert "billing-database" in compose["services"]
    assert "billing-cache" in compose["services"]
    # Historical offsets: PostgreSQL at +1, Redis at +3.
    assert compose["services"]["billing-database"]["ports"] == ["8011:5432"]
    assert compose["services"]["billing-cache"]["ports"] == ["8013:6379"]


def test_migrating_preserves_every_port() -> None:
    workspace = _workspace()
    workspace.add(_service("billing", 8010, datastores=["database", "cache"]))
    before = build_workspace_compose(workspace, with_caddy=False)

    workspace.migrate_resources()

    after = build_workspace_compose(workspace, with_caddy=False)
    assert sorted(before["services"]) == sorted(after["services"])
    assert (
        before["services"]["billing-database"]["ports"]
        == (after["services"]["billing-database"]["ports"])
    )
    assert workspace.services[0].datastores == []
    assert {b.resource for b in workspace.services[0].uses} == {
        "billing-database",
        "billing-cache",
    }


def test_migrating_twice_changes_nothing() -> None:
    workspace = _workspace()
    workspace.add(_service("billing", 8010, datastores=["database"]))
    workspace.migrate_resources()
    once = workspace.render()
    workspace.migrate_resources()
    assert workspace.render() == once


# -- validation --------------------------------------------------------


def test_a_binding_to_a_resource_that_does_not_exist() -> None:
    workspace = _workspace()
    service = _service("billing", 8010)
    workspace.add(service)
    service.uses.append(Binding(resource="ghost"))
    assert any("not a resource" in p for p in workspace.validate())


def test_two_resources_cannot_share_one_variable() -> None:
    workspace = _workspace()
    workspace.add(_service("billing", 8010))
    workspace.add_resource(Resource(name="core-db", type="postgres", port=8900))
    workspace.add_resource(Resource(name="analytics-db", type="postgres", port=8901))
    workspace.link("billing", "core-db")

    with pytest.raises(ValueError, match="already binds"):
        workspace.link("billing", "analytics-db")


def test_a_port_claimed_twice_is_reported() -> None:
    workspace = _workspace()
    workspace.add(_service("billing", 8010))
    workspace.add_resource(Resource(name="core-db", type="postgres", port=8010))
    assert any("port 8010 is claimed" in p for p in workspace.validate())


def test_an_unused_resource_is_reported() -> None:
    workspace = _workspace()
    workspace.add_resource(Resource(name="orphan", type="redis", port=8900))
    assert any("nothing uses it" in p for p in workspace.validate())


def test_a_healthy_workspace_has_no_problems() -> None:
    workspace = _workspace()
    workspace.add(_service("billing", 8010))
    workspace.add_resource(Resource(name="core-db", type="postgres", port=8900))
    workspace.link("billing", "core-db")
    assert workspace.validate() == []


def test_an_unknown_resource_type_is_refused() -> None:
    with pytest.raises(ValueError, match="Unknown resource type"):
        Resource(name="x", type="cassandra", port=9000)


# -- round-trip and rendering ------------------------------------------


def test_the_file_round_trips(tmp_path: Path) -> None:
    workspace = _workspace()
    workspace.add(_service("billing", 8010))
    workspace.add_resource(Resource(name="core-db", type="postgres", port=8900, database="core"))
    workspace.link("billing", "core-db")

    path = tmp_path / "jfast.workspace.toml"
    workspace.save(path)
    tomllib.loads(path.read_text(encoding="utf-8"))  # valid TOML

    reloaded = Workspace.load(path)
    assert [r.name for r in reloaded.resources] == ["core-db"]
    assert reloaded.resources[0].database == "core"
    assert [b.resource for b in reloaded.services[0].uses] == ["core-db"]
    assert reloaded.environment_for(reloaded.services[0]) == workspace.environment_for(
        workspace.services[0]
    )


def test_ports_are_allocated_outside_the_service_range() -> None:
    workspace = _workspace()
    workspace.add(_service("billing", 8010))
    first = workspace.next_resource_port()
    assert first >= workspace.base_port + 900
    workspace.add_resource(Resource(name="a", type="redis", port=first))
    assert workspace.next_resource_port() == first + 1


def test_the_graph_labels_edges_with_the_variable() -> None:
    workspace = _workspace()
    workspace.add(_service("billing", 8010))
    workspace.add_resource(Resource(name="core-db", type="postgres", port=8900))
    workspace.link("billing", "core-db")

    mermaid = render_graph(workspace, output_format="mermaid")
    assert "flowchart LR" in mermaid
    assert "billing -->|JFAST_DB_DSN| core_db" in mermaid

    dot = render_graph(workspace, output_format="dot")
    assert 'label="JFAST_DB_DSN"' in dot


def test_describe_carries_the_environment() -> None:
    workspace = _workspace()
    workspace.add(_service("billing", 8010))
    workspace.add_resource(Resource(name="core-db", type="postgres", port=8900))
    workspace.link("billing", "core-db")

    described = workspace.describe()
    assert described["resources"][0]["name"] == "core-db"
    assert "JFAST_DB_DSN" in described["services"][0]["environment"]
