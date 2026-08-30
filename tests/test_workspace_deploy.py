"""Workspace-level compose and Caddyfile generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from jfastframework.deploy.workspace import (
    build_workspace_compose,
    render_caddyfile,
    render_workspace_compose,
)
from jfastframework.workspace import ServiceEntry, Workspace


def ws(*services: ServiceEntry) -> Workspace:
    workspace = Workspace(name="cometax")
    for service in services:
        workspace.add(service)
    return workspace


def on_disk(root: Path, *services: ServiceEntry) -> Workspace:
    """A workspace whose services have a directory and a ``jfast.toml``.

    The plugin graph lives per service, in that file. A workspace held only in
    memory has nowhere to read it from, which is why every plugin assertion
    needs a real directory.
    """
    workspace = Workspace(name="cometax", file=root / "jfast.workspace.toml")
    for service in services:
        workspace.add(service)
    workspace.save()
    return workspace


def service_config(root: Path, service: ServiceEntry, body: str = "") -> None:
    directory = root / service.path
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "jfast.toml").write_text(body, encoding="utf-8")


def api(name: str, port: int, **kwargs: object) -> ServiceEntry:
    return ServiceEntry(name=name, kind="api", port=port, path=name, **kwargs)  # type: ignore[arg-type]


def spa(name: str, port: int) -> ServiceEntry:
    return ServiceEntry(name=name, kind="spa", port=port, path=name, frontend="vue")


def gateway(port: int) -> ServiceEntry:
    return ServiceEntry(name="gateway", kind="gateway", port=port, path="gateway")


# -- compose ------------------------------------------------------------


def test_every_backend_becomes_a_service() -> None:
    compose = build_workspace_compose(ws(api("billing", 8010), api("catalog", 8020)))
    assert "billing" in compose["services"]
    assert "catalog" in compose["services"]


def test_a_frontend_is_not_a_container() -> None:
    compose = build_workspace_compose(ws(api("billing", 8010), spa("admin", 8020)))
    # A built SPA is static files; Caddy serves them from ./dist. Running a
    # Node container in production to serve them is a process nobody needs.
    assert "admin" not in compose["services"]


def test_declared_datastores_become_containers() -> None:
    compose = build_workspace_compose(ws(api("billing", 8010, datastores=["database", "cache"])))
    assert "billing-database" in compose["services"]
    assert "billing-cache" in compose["services"]


def test_datastores_are_namespaced_per_service() -> None:
    compose = build_workspace_compose(
        ws(
            api("billing", 8010, datastores=["database"]),
            api("catalog", 8020, datastores=["database"]),
        )
    )
    # Two services asking for `database` get two instances. Splitting services
    # to share one database is a split that bought nothing.
    assert "billing-database" in compose["services"]
    assert "catalog-database" in compose["services"]


def test_datastore_ports_follow_the_block_offsets() -> None:
    compose = build_workspace_compose(
        ws(api("billing", 8010, datastores=["database", "cache", "qdrant"]))
    )
    assert compose["services"]["billing-database"]["ports"] == ["8011:5432"]
    assert compose["services"]["billing-cache"]["ports"] == ["8013:6379"]
    assert compose["services"]["billing-qdrant"]["ports"] == ["8017:6333"]


def test_a_service_waits_for_a_healthy_database() -> None:
    compose = build_workspace_compose(ws(api("billing", 8010, datastores=["database"])))
    assert compose["services"]["billing"]["depends_on"]["billing-database"] == {
        "condition": "service_healthy"
    }


def test_grpc_publishes_its_own_port() -> None:
    compose = build_workspace_compose(ws(api("edge", 8010, grpc=True)))
    assert compose["services"]["edge"]["ports"] == ["8010:8010", "8019:8019"]


def test_caddy_can_be_left_out() -> None:
    compose = build_workspace_compose(ws(api("billing", 8010)), with_caddy=False)
    assert "caddy" not in compose["services"]


def test_the_rendered_compose_is_valid_yaml() -> None:
    yaml = pytest.importorskip("yaml")
    workspace = ws(api("billing", 8010, datastores=["database"]), spa("admin", 8020))
    parsed = yaml.safe_load(render_workspace_compose(workspace))

    assert parsed["services"]["billing"]["build"]["context"] == "./billing"
    assert "billing_database_data" in parsed["volumes"]


# -- the plugin graph ---------------------------------------------------


def test_a_plugin_that_declares_infra_becomes_a_container(tmp_path: Path) -> None:
    """`events` owns a broker. Nothing in the resource graph can say so."""
    billing = api("billing", 8010)
    workspace = on_disk(tmp_path, billing)
    service_config(
        tmp_path,
        billing,
        '[app]\nname = "billing"\nport = 8010\n\n[plugins]\nenabled = ["events"]\n',
    )

    compose = build_workspace_compose(workspace, with_caddy=False)

    assert "kafka" in compose["services"], "the broker the plugin declares is missing"
    # Inside billing's ten-port block, like every other offset. Which internal
    # port it maps to is the plugin's business, not this generator's.
    published = compose["services"]["kafka"]["ports"][0].partition(":")[0]
    assert 8010 <= int(published) < 8020
    assert "kafka" in compose["services"]["billing"]["depends_on"]
    assert "kafka_data" in compose["volumes"]
    # The plugin was told which base port it is published on, so the address it
    # advertises to clients outside the compose network is one they can reach.
    environment = compose["services"]["kafka"].get("environment", {})
    assert any(published in value for value in environment.values())


def test_the_resource_graph_still_owns_the_datastores(tmp_path: Path) -> None:
    """`database` declares infra too, and the resource graph already has it.

    Emitting both would put a second, nameless PostgreSQL beside the one the
    workspace file declares -- and only one of them has a DSN pointing at it.
    """
    billing = api("billing", 8010, datastores=["database"])
    workspace = on_disk(tmp_path, billing)
    service_config(
        tmp_path,
        billing,
        '[app]\nname = "billing"\nport = 8010\n\n[plugins]\nenabled = ["database"]\n',
    )

    compose = build_workspace_compose(workspace, with_caddy=False)

    assert "billing-database" in compose["services"]
    assert "postgres" not in compose["services"]


def test_one_broker_when_two_services_publish_to_it(tmp_path: Path) -> None:
    """The broker advertises its own container name, so there can be one."""
    billing = api("billing", 8010)
    catalog = api("catalog", 8020)
    workspace = on_disk(tmp_path, billing, catalog)
    for service in (billing, catalog):
        service_config(
            tmp_path,
            service,
            f'[app]\nname = "{service.name}"\nport = {service.port}\n\n'
            '[plugins]\nenabled = ["events"]\n',
        )

    compose = build_workspace_compose(workspace, with_caddy=False)

    assert len([name for name in compose["services"] if name == "kafka"]) == 1
    assert "kafka" in compose["services"]["billing"]["depends_on"]
    assert "kafka" in compose["services"]["catalog"]["depends_on"]


def test_a_plugin_that_cannot_be_inspected_is_loud(tmp_path: Path) -> None:
    """Silence is the defect. A plugin we cannot read may own a container."""
    billing = api("billing", 8010)
    workspace = on_disk(tmp_path, billing)
    service_config(
        tmp_path,
        billing,
        '[app]\nname = "billing"\nport = 8010\n\n[plugins]\nenabled = ["nosuchplugin"]\n',
    )

    with pytest.warns(UserWarning, match="nosuchplugin"):
        build_workspace_compose(workspace, with_caddy=False)


def test_local_storage_disks_survive_a_rebuild(tmp_path: Path) -> None:
    """Uploads written into the image are gone on the next `docker build`."""
    billing = api("billing", 8010)
    workspace = on_disk(tmp_path, billing)
    service_config(
        tmp_path,
        billing,
        '[app]\nname = "billing"\nport = 8010\n\n[plugins]\nenabled = ["storage"]\n',
    )

    compose = build_workspace_compose(workspace, with_caddy=False)

    mounts = compose["services"]["billing"]["volumes"]
    assert "billing_public_data:/app/storage/public" in mounts
    assert "billing_private_data:/app/storage/private" in mounts
    assert "billing_public_data" in compose["volumes"]


def test_postgres_gets_more_than_64mb_of_shared_memory() -> None:
    """A parallel query needs /dev/shm; 64 MB is where it starts failing."""
    compose = build_workspace_compose(
        ws(api("billing", 8010, datastores=["database"])), with_caddy=False
    )
    assert compose["services"]["billing-database"]["shm_size"]


# -- Caddyfile ----------------------------------------------------------


def test_one_backend_is_served_under_api() -> None:
    caddyfile = render_caddyfile(ws(api("billing", 8010)))
    # /api whether or not there is a gateway, so the frontend's production
    # build keeps working the day one appears.
    assert "handle /api/* {" in caddyfile
    assert "reverse_proxy billing:8010" in caddyfile


def test_a_gateway_takes_over_the_api_prefix() -> None:
    caddyfile = render_caddyfile(ws(api("billing", 8010), api("catalog", 8020), gateway(8030)))
    assert "reverse_proxy gateway:8030" in caddyfile
    assert "reverse_proxy billing:8010" not in caddyfile


def test_several_backends_without_a_gateway_are_namespaced_under_api() -> None:
    caddyfile = render_caddyfile(ws(api("billing", 8010), api("catalog", 8020)))
    assert "handle /api/billing/* {" in caddyfile
    assert "handle /api/catalog/* {" in caddyfile


def test_the_spa_gets_try_files_so_refresh_works() -> None:
    caddyfile = render_caddyfile(ws(api("billing", 8010), spa("admin", 8020)))
    # Without try_files, a hard refresh on /orders is a 404 from the file
    # server rather than the SPA's own route.
    assert "try_files {path} /index.html" in caddyfile


def test_local_development_disables_automatic_https() -> None:
    assert "auto_https off" in render_caddyfile(ws(api("billing", 8010)))


def test_production_leaves_automatic_https_on() -> None:
    caddyfile = render_caddyfile(
        ws(api("billing", 8010)), hostname="app.example.com", local_dev=False
    )
    assert "auto_https off" not in caddyfile
    assert caddyfile.count("app.example.com {") == 1
