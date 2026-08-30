"""Every lifecycle command is reachable through the real CLI.

Each command has its own suite driving a locally built Typer app. That proves
the command works; it does not prove `main.py` attached it, nor that it landed
under the right parent. `contracts explain` registers into the `contracts`
group when it finds one and becomes a top-level `jfast explain` when it does
not -- both spellings run, and only one is the documented interface.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jfastframework.cli.main import app

runner = CliRunner()


TOP_LEVEL = [
    "inspect",
    "analyze",
    "graph",
    "check",
    "next",
    "upgrade",
]

NESTED = [
    ("contracts", "explain"),
    ("contracts", "diff"),
    ("contracts", "check"),
    ("migration", "check"),
    ("migration", "plan"),
    ("ai", "context"),
]


@pytest.mark.parametrize("command", TOP_LEVEL)
def test_a_top_level_command_is_reachable(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(("group", "command"), NESTED)
def test_a_nested_command_is_reachable_under_its_group(group: str, command: str) -> None:
    result = runner.invoke(app, [group, command, "--help"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(("group", "command"), NESTED)
def test_a_nested_command_did_not_land_at_the_top_level(group: str, command: str) -> None:
    """The failure mode a per-module test cannot see.

    `explain.register` falls back to attaching directly to `app` when no
    `contracts` group exists yet. Registering it before the group is created
    gives a `jfast explain` that works perfectly and is not the interface the
    documentation describes.
    """
    if command in TOP_LEVEL:
        pytest.skip(f"{command!r} is legitimately a top-level command too")
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code != 0, f"{command!r} is reachable without {group!r}: {result.output}"


def test_the_help_lists_every_group() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in ("contracts", "migration", "ai", "workspace", "deploy", "new", "plugins"):
        assert group in result.output, result.output
