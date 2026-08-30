"""``jfast upgrade --check``: what breaks if this project moves to the installed
framework version.

Not release notes. The manifest in :mod:`jfastframework.upgrades` carries a
``detect`` per change, and a change nothing in the project can trigger is left
out of the report entirely -- because a warning that does not apply is how
people learn to skip the output, and the next warning goes with it.

``--apply`` is deliberately unimplemented. Rewriting somebody's models,
contract and settings needs a rollback story -- a clean tree to start from, a
diff to review, a way back when the rewrite is wrong -- and none of that exists
here. A wrong automatic edit costs more than the manual one it saved.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from typing import Any

import typer

from jfastframework import upgrades
from jfastframework.cli import ui
from jfastframework.cli.exits import Code
from jfastframework.project import load

__all__ = ["register"]

# Bound here rather than inline in the signature: a call in a default argument
# is what ruff's B008 exists to catch, and main.py only silences it for itself.
_CHECK = typer.Option(False, "--check", help="Report what breaks. The only mode there is.")
_APPLY = typer.Option(False, "--apply", help="Not implemented: see the note below.")
_PATH = typer.Option(Path("."), "--path", "-p", help="Project root.")
_JSON = typer.Option(False, "--json", help="Machine-readable output.")


def register(app: typer.Typer) -> None:
    """Attach `upgrade` to *app*.

    A function rather than a decorator on the command, so `main.py` stays one
    line per command and several of these can be wired without every author
    editing the same file.
    """
    app.command("upgrade")(upgrade)


def upgrade(
    check: bool = _CHECK,
    apply: bool = _APPLY,
    path: Path = _PATH,
    json_out: bool = _JSON,
) -> None:
    """What breaks if this project moves to the installed framework version.

    Compares the version the project pins -- `requirements.txt`, or the
    `.jfast-template` stamp a scaffold left behind -- against the installed
    `jfastframework`, and reports the changes in between that this project can
    actually feel. A change nothing here can trigger is not printed: the
    timezone migration only when a model mixes in `TimestampMixin`, the refresh
    token break only when this service issues tokens.

    Exits 7 when something applies, 0 when nothing does, so CI can gate on it.

    `--apply` does not exist and is not planned for this release. Automatic
    rewriting of a project needs a rollback story this does not have.
    """
    if apply:
        typer.echo(
            "--apply is not implemented. Rewriting a project automatically needs a way "
            "back when the rewrite is wrong, and there is none here. Run --check and "
            "make the edits it names."
        )
        raise typer.Exit(Code.USAGE)

    root = path.resolve()
    if not (root / "jfast.toml").is_file():
        typer.echo(f"no jfast.toml in {root}. Point --path at a service.")
        raise typer.Exit(Code.CONFIG)

    pin = upgrades.pinned_version(root)
    if pin is None:
        typer.echo(
            "cannot tell which framework version this project is on: no jfastframework "
            "line in requirements.txt and no .jfast-template stamp. Add the pin, then "
            "run this again."
        )
        raise typer.Exit(Code.CONFIG)

    from jfastframework import __version__

    try:
        older = upgrades.parse_version(pin.version) < upgrades.parse_version(__version__)
        newer = upgrades.parse_version(pin.version) > upgrades.parse_version(__version__)
    except ValueError as error:
        typer.echo(str(error))
        raise typer.Exit(Code.CONFIG) from error

    project = load(root)
    found = (
        upgrades.applicable(project, current=pin.version, installed=__version__) if older else []
    )
    payload = _payload(project.name, pin, __version__, found, newer=newer)

    if json_out:
        typer.echo(jsonlib.dumps(payload, indent=2, default=str))
    else:
        typer.echo(_render(pin, __version__, found, newer=newer))

    if found or newer:
        raise typer.Exit(Code.COMPATIBILITY)


def _payload(
    name: str,
    pin: upgrades.Pin,
    installed: str,
    found: upgrades.Applicable,
    *,
    newer: bool,
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for change, _ in found:
        counts[change.kind] = counts.get(change.kind, 0) + 1
    return {
        "schema_version": "1",
        "project": name,
        "from": pin.version,
        "from_source": pin.source,
        "to": installed,
        # A project ahead of the installed framework is not "fine": it is
        # running code the installed wheel does not contain.
        "project_is_newer": newer,
        "ok": not found and not newer,
        "counts": counts,
        "changes": [change.describe(affected) for change, affected in found],
    }


def _render(
    pin: upgrades.Pin,
    installed: str,
    found: upgrades.Applicable,
    *,
    newer: bool,
) -> str:
    G = ui.G
    lines = [f"  {pin.version} {G.arrow} {installed}   (pinned in {pin.source})", ""]

    if newer:
        lines.append(
            f"  {G.cross} this project pins a version newer than the installed "
            f"jfastframework {installed}."
        )
        lines.append("     Install what it pins, or nothing here describes the code you run.")
        return "\n".join(lines)

    if not found:
        lines.append(f"  {G.tick} nothing between those versions affects this project")
        return "\n".join(lines)

    for change, affected in found:
        lines.append(f"  {G.cross} {change.code}  [{change.kind}, {change.version}]")
        lines.append(f"      {change.summary}")
        lines.append("")
        lines.extend(_wrapped(change.detail))
        if affected:
            lines.append("")
            lines.append("      in this project:")
            for item in affected:
                first, *rest = item.split("\n")
                lines.append(f"        {first}")
                lines.extend(f"          {line}" for line in rest)
        lines.append("")
        lines.append(f"      {G.arrow} fix")
        lines.extend(_wrapped(change.remedy, indent=8))
        lines.append("")

    breaking = sum(1 for change, _ in found if change.kind == "breaking")
    lines.append(f"  {len(found)} apply here, {breaking} breaking.")
    return "\n".join(lines)


def _wrapped(text: str, *, indent: int = 6, width: int = 78) -> list[str]:
    """Paragraph text at a fixed width.

    Wrapped here rather than left to the terminal: the report is read as often
    from a CI log or a pasted transcript as from a tty, and neither reflows.
    """
    import textwrap

    pad = " " * indent
    return textwrap.wrap(text, width=width - indent, initial_indent=pad, subsequent_indent=pad)
