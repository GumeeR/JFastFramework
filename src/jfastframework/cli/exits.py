"""What the CLI's exit codes mean.

A script that has to read English to find out why a command failed is a script
that breaks when the wording changes. These codes are the stable half of the
interface: CI can branch on "the contract was violated" versus "the config does
not parse" without parsing a single line of output.

They are part of the public API. A code never changes meaning; a new failure
mode gets a new number.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = ["MEANING", "Code"]


class Code(IntEnum):
    """Exit codes. `raise typer.Exit(Code.CONTRACT)` at the call site."""

    OK = 0
    """Nothing to report."""

    VALIDATION = 1
    """The project is inconsistent: a check found something wrong with it."""

    CONFIG = 2
    """`jfast.toml`, `contracts.toml` or the workspace file is missing or unreadable."""

    ENVIRONMENT = 3
    """Something outside the project is missing: Docker, a container, a database."""

    MIGRATION = 4
    """A migration failed, or was refused as unsafe."""

    CONTRACT = 5
    """The code violates its declared contract."""

    USAGE = 6
    """The command was called wrongly: bad argument, unknown name."""

    COMPATIBILITY = 7
    """The project and the installed framework version do not agree."""


MEANING: dict[int, str] = {
    Code.OK: "success",
    Code.VALIDATION: "validation failure",
    Code.CONFIG: "configuration error",
    Code.ENVIRONMENT: "environment error",
    Code.MIGRATION: "migration risk or failure",
    Code.CONTRACT: "contract violation",
    Code.USAGE: "user input error",
    Code.COMPATIBILITY: "compatibility or version error",
}
