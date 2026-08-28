"""Workspace: port allocation, gateway thresholds, and what a frontend calls."""

from __future__ import annotations

from pathlib import Path

import pytest

from jfastframework.workspace import (
    PORT_BLOCK_SIZE,
    ServiceEntry,
    Workspace,
)


def ws(**kwargs: object) -> Workspace:
    return Workspace(name="cometax", **kwargs)  # type: ignore[arg-type]


def api(name: str, port: int) -> ServiceEntry:
    return ServiceEntry(name=name, kind="api", port=port, path=name)


def test_first_service_gets_a_block_above_the_base_port() -> None:
    workspace = ws()
    assert workspace.next_port() == workspace.base_port + PORT_BLOCK_SIZE


def test_blocks_do_not_overlap() -> None:
    workspace = ws()
    first = workspace.next_port()
    workspace.add(api("billing", first))
    second = workspace.next_port()
    # A plugin inside `billing` claims first+1 (postgres) and first+7 (qdrant);
    # handing out first+1 to the next service would collide with those.
    assert second - first == PORT_BLOCK_SIZE


def test_a_taken_port_is_refused_with_the_next_free_block() -> None:
    workspace = ws()
    workspace.add(api("billing", 8010))
    with pytest.raises(ValueError, match="already taken by 'billing'"):
        workspace.add(api("catalog", 8010))


def test_a_duplicate_name_is_refused_unless_replacing() -> None:
    workspace = ws()
    workspace.add(api("billing", 8010))
    with pytest.raises(ValueError, match="already in the workspace"):
        workspace.add(api("billing", 8020))
    workspace.add(api("billing", 8020), replace=True)
    assert workspace.get("billing").port == 8020  # type: ignore[union-attr]


def test_one_backend_does_not_need_a_gateway() -> None:
    workspace = ws()
    workspace.add(api("billing", 8010))
    assert workspace.needs_gateway() is False


def test_two_backends_need_a_gateway() -> None:
    workspace = ws()
    workspace.add(api("billing", 8010))
    workspace.add(api("catalog", 8020))
    assert workspace.needs_gateway() is True


def test_a_frontend_does_not_count_as_a_backend() -> None:
    workspace = ws()
    workspace.add(api("billing", 8010))
    workspace.add(ServiceEntry(name="admin", kind="spa", port=8020, path="admin", frontend="vue"))
    assert workspace.needs_gateway() is False


def test_an_existing_gateway_is_not_regenerated() -> None:
    workspace = ws()
    workspace.add(api("billing", 8010))
    workspace.add(api("catalog", 8020))
    workspace.add(ServiceEntry(name="gateway", kind="gateway", port=8030, path="gateway"))
    assert workspace.needs_gateway() is False


def test_the_frontend_calls_the_single_backend_when_there_is_no_gateway() -> None:
    workspace = ws()
    workspace.add(api("billing", 8010))
    assert workspace.api_base_url() == "http://localhost:8010"


def test_the_frontend_calls_the_gateway_once_there_is_one() -> None:
    workspace = ws()
    workspace.add(api("billing", 8010))
    workspace.add(api("catalog", 8020))
    workspace.add(ServiceEntry(name="gateway", kind="gateway", port=8030, path="gateway"))
    # This is the value that goes stale by hand the day a gateway appears.
    assert workspace.api_base_url() == "http://localhost:8030"


def test_internal_urls_use_the_compose_service_name() -> None:
    entry = api("billing", 8010)
    assert entry.internal_url == "http://billing:8010"
    assert entry.prefix == "/billing"


def test_underscores_become_hyphens_in_the_gateway_prefix() -> None:
    assert api("payment_methods", 8010).prefix == "/payment-methods"


def test_a_workspace_round_trips_through_its_file(tmp_path: Path) -> None:
    workspace = ws(file=tmp_path / "jfast.workspace.toml")
    workspace.add(api("billing", 8010))
    workspace.add(ServiceEntry(name="admin", kind="spa", port=8020, path="admin", frontend="react"))
    workspace.save()

    reloaded = Workspace.load(workspace.file)  # type: ignore[arg-type]
    assert reloaded.name == "cometax"
    assert [s.name for s in reloaded.services] == ["billing", "admin"]
    assert reloaded.get("admin").frontend == "react"  # type: ignore[union-attr]


def test_find_walks_up_from_a_nested_directory(tmp_path: Path) -> None:
    root = tmp_path / "cometax"
    nested = root / "billing" / "modules"
    nested.mkdir(parents=True)
    ws(file=root / "jfast.workspace.toml").save()

    found = Workspace.find(nested)
    assert found == root / "jfast.workspace.toml"


def test_find_returns_none_outside_a_workspace(tmp_path: Path) -> None:
    assert Workspace.find(tmp_path) is None
