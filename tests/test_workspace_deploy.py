"""Workspace-level compose and Caddyfile generation."""

from __future__ import annotations

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
