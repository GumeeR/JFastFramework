"""What the two compose generators owe each other.

``jfast deploy compose`` writes one service's compose file; ``jfast workspace
compose`` writes a whole workspace's. They answer different questions, so they
are allowed to differ -- but only where the difference is load-bearing, and only
where it is written down. Everything else has to be the same, or a service
changes behaviour by being deployed through the other command.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jfastframework.deploy import build_compose, render_compose
from jfastframework.deploy.workspace import build_workspace_compose, render_workspace_compose
from jfastframework.plugins.builtin.storage import StoragePlugin
from jfastframework.testing import make_config
from jfastframework.workspace import ServiceEntry, Workspace


def api(name: str, port: int, **kwargs: object) -> ServiceEntry:
    return ServiceEntry(name=name, kind="api", port=port, path=name, **kwargs)  # type: ignore[arg-type]


def on_disk(root: Path, name: str, *services: ServiceEntry) -> Workspace:
    root.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(name=name, file=root / "jfast.workspace.toml")
    for service in services:
        workspace.add(service)
    workspace.save()
    for service in services:
        directory = root / service.path
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "jfast.toml").write_text(
            f'[app]\nname = "{service.name}"\nport = {service.port}\n\n'
            '[plugins]\nenabled = ["storage"]\n',
            encoding="utf-8",
        )
    return workspace


# -- what both generators must do the same way --------------------------


def test_local_storage_disks_survive_a_rebuild_on_the_single_service_path() -> None:
    """The same guarantee `jfast workspace compose` already gives.

    Without a volume the uploads live in the container's own filesystem and the
    next `docker build` throws them away, while the rows referencing them stay.
    A service must not lose that by being deployed through the other command.
    """
    compose = build_compose(make_config(app_name="billing", port=8010), [StoragePlugin()])

    mounts = compose["services"]["api"]["volumes"]
    assert "billing_public_data:/app/storage/public" in mounts
    assert "billing_private_data:/app/storage/private" in mounts
    assert "billing_public_data" in compose["volumes"]


def test_the_two_generators_name_a_storage_volume_the_same_way(tmp_path: Path) -> None:
    """One naming rule, so `docker volume ls` reads the same either way."""
    single = build_compose(make_config(app_name="billing", port=8010), [StoragePlugin()])
    workspace = build_workspace_compose(
        on_disk(tmp_path, "cometax", api("billing", 8010)), with_caddy=False
    )

    assert single["services"]["api"]["volumes"] == workspace["services"]["billing"]["volumes"]


def test_a_disk_that_is_not_local_has_nothing_to_keep() -> None:
    """S3 and MinIO hold the bytes themselves; a volume for them keeps nothing."""
    plugin = StoragePlugin({"disks": {"media": {"driver": "s3", "root": "media"}}})
    compose = build_compose(make_config(app_name="billing", port=8010), [plugin])

    assert "volumes" not in compose["services"]["api"]
    assert "volumes" not in compose


# -- the difference that stays, because it is load-bearing --------------


def test_the_generators_disagree_about_datastore_names_on_purpose(tmp_path: Path) -> None:
    """Documented in docs/deploy.md, and asserted here so it cannot drift.

    A single service owns one PostgreSQL, so the plugin may call it `postgres`
    and read `POSTGRES_PASSWORD`. A workspace owns any number, so each is named
    and carries its own secret -- one shared password would make a leak
    anywhere a leak everywhere. Neither name can be given to the other.
    """
    from jfastframework.plugins.builtin.database import DatabasePlugin

    single = build_compose(make_config(app_name="billing", port=8010), [DatabasePlugin()])
    assert "postgres" in single["services"]
    assert (
        single["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"]
        == "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}"
    )

    workspace = build_workspace_compose(
        Workspace(name="cometax", services=[api("billing", 8010, datastores=["database"])]),
        with_caddy=False,
    )
    assert "billing-database" in workspace["services"]
    assert (
        workspace["services"]["billing-database"]["environment"]["POSTGRES_PASSWORD"]
        == "${BILLING_DATABASE_PASSWORD:?set BILLING_DATABASE_PASSWORD}"
    )


# -- one generator, two projects, at the same time -----------------------


def _container_names(compose: dict[str, object]) -> list[str]:
    services: dict[str, dict[str, object]] = compose["services"]  # type: ignore[assignment]
    return [name for name, entry in services.items() if "container_name" in entry]


def test_a_workspace_compose_file_pins_no_container_name(tmp_path: Path) -> None:
    """A pinned name is global to the daemon, so a second copy of the same
    workspace cannot start: `the container name "/social-database" is already
    in use`. Compose derives one per project instead, and `-p` renames it."""
    workspace = on_disk(tmp_path, "cometax", api("social", 8010, datastores=["database"]))

    compose = build_workspace_compose(workspace)

    assert _container_names(compose) == []


def test_a_single_service_compose_file_pins_no_container_name() -> None:
    compose = build_compose(make_config(app_name="billing", port=8010), [StoragePlugin()])

    assert _container_names(compose) == []


def test_neither_rendered_file_names_a_container_globally(tmp_path: Path) -> None:
    """The dict assertions above would still pass if the serialiser put the
    name back. What separates two copies is the compose project name, passed at
    `docker compose -p`, and nothing in the file may override it."""
    workspace = render_workspace_compose(on_disk(tmp_path, "cometax", api("social", 8010)))
    single = render_compose(build_compose(make_config(app_name="social", port=8010), []))

    assert "container_name" not in workspace
    assert "container_name" not in single


def test_the_rendered_files_are_still_valid_yaml(tmp_path: Path) -> None:
    yaml = pytest.importorskip("yaml")
    single = yaml.safe_load(
        render_compose(build_compose(make_config(app_name="billing", port=8010), [StoragePlugin()]))
    )
    workspace = yaml.safe_load(
        render_workspace_compose(on_disk(tmp_path, "cometax", api("billing", 8010)))
    )

    assert single["services"]["api"]["build"] == "."
    assert workspace["services"]["billing"]["build"]["context"] == "./billing"
