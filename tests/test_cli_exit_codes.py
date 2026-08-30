"""A command that reports failure has to fail the process, not just say so.

typer 0.27.1 against click 8.5.0 turned ``typer.Exit(1)`` into a zero exit
status. Every `jfast` command that signals failure by exit code went green in
CI, `contracts check` included, and no test noticed: they all asserted on the
message, which was still correct. So these assert on the status and nothing
else -- a test that reads stdout cannot catch this class of regression.
"""

from __future__ import annotations

import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jfastframework.cli.main import app

runner = CliRunner()


def test_typer_exit_reaches_the_process_status() -> None:
    # The narrowest possible statement of the broken behaviour, so a bad
    # typer/click pair fails here rather than somewhere confusing.
    import typer

    probe = typer.Typer()

    @probe.command()
    def fails() -> None:
        raise typer.Exit(1)

    assert runner.invoke(probe, []).exit_code == 1


@pytest.mark.parametrize(
    "command",
    [
        ["contracts", "check"],
        ["workspace", "list"],
        ["workspace", "gateway"],
        ["workspace", "env"],
    ],
)
def test_commands_that_cannot_find_their_project_exit_non_zero(
    command: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, command)
    assert result.exit_code != 0, f"{command} reported failure but exited 0:\n{result.output}"


def test_a_second_workspace_init_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["workspace", "init", "demo"]).exit_code == 0

    result = runner.invoke(app, ["workspace", "init", "demo"])
    assert result.exit_code != 0, result.output


def test_the_installed_console_script_fails_too(tmp_path: Path) -> None:
    """The real entry point, in a real process.

    CliRunner and ``jfast`` can disagree: the runner catches the exception
    itself, while the console script goes through click's standalone mode.
    Only this one proves what CI's shell sees.
    """
    result = subprocess.run(  # nosec B603
        [sys.executable, "-m", "jfastframework.cli.main", "contracts", "check"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
