"""The dotfiles the CLI writes next to a workspace, and the two that went missing.

Both cases here failed silently rather than loudly, which is why they shipped:
a secret landed in an unignored file while the CLI said it was ignored, and a
compose file referenced an ``env_file`` nothing generated.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from jfastframework.cli.main import (
    _write_dockerignore,
    _write_service_envs,
    _write_workspace_secrets,
)
from jfastframework.deploy import render_dockerignore
from jfastframework.deploy.workspace import render_workspace_compose
from jfastframework.workspace import Resource, ServiceEntry, Workspace


@pytest.fixture
def workdir(tmp_path: Path) -> Iterator[Path]:
    """Run in a scratch directory: these functions write relative to the cwd."""
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(previous)


def test_saving_a_workspace_ignores_the_env_file_it_will_generate(workdir: Path) -> None:
    # `jfast start` builds a Workspace and calls save() directly rather than
    # going through `workspace init`, so the ignore rule has to live on the
    # path every creator takes.
    Workspace(name="shop", file=Path("jfast.workspace.toml")).save()

    ignore = workdir / ".gitignore"
    assert ignore.is_file(), "saving a workspace left no .gitignore"
    assert ".env" in ignore.read_text(encoding="utf-8").split()


def test_the_generated_secret_is_ignored_where_it_lands(workdir: Path) -> None:
    workspace = Workspace(name="shop", file=Path("jfast.workspace.toml"))
    workspace.add(ServiceEntry(name="shop", kind="api", port=8000, path="shop"))
    workspace.resources.append(
        Resource(name="shop_database", type="postgres", port=5432, database="shop")
    )
    workspace.save()

    assert _write_workspace_secrets(workspace) == 1
    assert "SHOP_DATABASE_PASSWORD" in (workdir / ".env").read_text(encoding="utf-8")
    assert ".env" in (workdir / ".gitignore").read_text(encoding="utf-8").split()


def test_an_existing_gitignore_keeps_its_rules(workdir: Path) -> None:
    (workdir / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    Workspace(name="shop", file=Path("jfast.workspace.toml")).save()

    rules = (workdir / ".gitignore").read_text(encoding="utf-8").split()
    assert "__pycache__/" in rules
    assert ".env" in rules


def test_saving_twice_does_not_repeat_the_rule(workdir: Path) -> None:
    workspace = Workspace(name="shop", file=Path("jfast.workspace.toml"))
    workspace.save()
    workspace.save()

    rules = (workdir / ".gitignore").read_text(encoding="utf-8").split()
    assert rules.count(".env") == 1


def _two_backends_and_a_gateway() -> Workspace:
    workspace = Workspace(name="demo", file=Path("jfast.workspace.toml"))
    workspace.add(ServiceEntry(name="billing", kind="api", port=8010, path="billing"))
    workspace.add(ServiceEntry(name="shipping", kind="api", port=8020, path="shipping"))
    workspace.add(ServiceEntry(name="gateway", kind="gateway", port=8030, path="gateway"))
    return workspace


def test_every_service_the_compose_file_names_gets_an_env_file(workdir: Path) -> None:
    # The gateway binds no resources, so its environment is empty -- but compose
    # treats a missing env_file as an error, not as an empty set.
    workspace = _two_backends_and_a_gateway()
    written = _write_service_envs(workspace)

    compose = render_workspace_compose(workspace)
    referenced = [
        line.strip().lstrip("- ").strip()
        for line in compose.splitlines()
        if line.strip().startswith("- ./") and line.strip().endswith("/.env")
    ]
    assert referenced, "the compose file names no env_file at all"

    missing = [path for path in referenced if not (workdir / path).is_file()]
    assert not missing, f"compose references env files nothing wrote: {missing}"
    assert Path("gateway/.env") in written


def test_a_service_with_no_bindings_still_gets_an_env_file(workdir: Path) -> None:
    workspace = _two_backends_and_a_gateway()
    gateway = next(s for s in workspace.services if s.name == "gateway")
    assert workspace.environment_for(gateway) == {}

    _write_service_envs(workspace)
    assert (workdir / "gateway" / ".env").is_file()


# -- the exclude list the image needs ----------------------------------
#
# The Dockerfile ends in `COPY . .`, and the first thing the CLI tells a new
# service to do is `cp .env.example .env`. Without a .dockerignore between
# those two facts, the filled-in secrets are in a layer of a distributable
# image, and deleting the file afterwards does not remove the layer.


def test_a_new_service_is_born_with_a_dockerignore(workdir: Path) -> None:
    written = _write_dockerignore(workdir / "billing", dry_run=False)

    assert [w.created for w in written] == [True]
    assert (workdir / "billing" / ".dockerignore").is_file()


def test_the_dockerignore_excludes_the_env_file_but_not_its_template() -> None:
    rules = render_dockerignore().split("\n")

    assert ".env" in rules
    # `.env.*` would take the example with it, so the negation has to be there
    # and has to come after the pattern that matched it.
    assert "!.env.example" in rules
    assert rules.index(".env.*") < rules.index("!.env.example")


@pytest.mark.parametrize("pattern", [".git", ".venv", "__pycache__", "storage"])
def test_the_dockerignore_excludes_what_never_belongs_in_an_image(pattern: str) -> None:
    assert pattern in render_dockerignore().split("\n")


def test_an_existing_dockerignore_is_not_overwritten(workdir: Path) -> None:
    # It is a file people edit. A generator that replaces an edited copy is
    # how a build silently starts shipping a directory somebody excluded.
    service = workdir / "billing"
    service.mkdir()
    (service / ".dockerignore").write_text("mine\n", encoding="utf-8")

    written = _write_dockerignore(service, dry_run=False)

    assert [w.created for w in written] == [False]
    assert (service / ".dockerignore").read_text(encoding="utf-8") == "mine\n"
