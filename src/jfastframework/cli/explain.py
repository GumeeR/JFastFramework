"""`jfast contracts explain` and `jfast contracts diff`.

The commands are thin on purpose: everything they say comes from
:mod:`jfastframework.contracts.explain`, which is a pure function from a
contract to a value and is tested without a Typer runner. What lives here is
the shape of the answer on a terminal, and the exit codes.

The output is written for an agent as much as for a person, which decides two
things about it. Every answer names the file and line that declares the rule,
so the claim can be checked rather than believed. And every answer states what
waiving costs, both halves of it -- because the cheap silent fix is editing
``contracts.toml``, and an agent that meets the inline waiver first and the
silent one never will reach for whichever it found.
"""

from __future__ import annotations

import json as jsonlib
import textwrap
from pathlib import Path
from typing import Annotated, Any

import typer

from jfastframework.cli.exits import Code
from jfastframework.contracts.explain import (
    RULES,
    WAIVER_COST,
    ArchitectureDiff,
    Explanation,
    diff,
    explain,
)
from jfastframework.contracts.model import CONTRACTS_FILE, Contract

__all__ = ["diff_command", "explain_command", "register"]

_WIDTH = 96
_LABEL = 11


def register(app: typer.Typer) -> None:
    """Attach `explain` and `diff` to *app*.

    Called from `main.py`, which this module deliberately does not touch: four
    sibling commands are being added to it at the same time and every one of
    them would collide there.

    If *app* already carries a `contracts` group the commands land in it, so a
    caller that passes the root app gets `jfast contracts explain` rather than
    a second top-level `jfast explain` that means something else.
    """
    target = _contracts_group(app) or app
    target.command("explain")(explain_command)
    target.command("diff")(diff_command)


def _contracts_group(app: typer.Typer) -> typer.Typer | None:
    for group in app.registered_groups:
        if group.name == "contracts" and group.typer_instance is not None:
            return group.typer_instance
    return None


def _require_contract(given: Path | None) -> tuple[Contract, Path]:
    """The contract to answer from, and the root its paths are relative to."""
    source = given
    if source is not None and source.is_dir():
        source = source / CONTRACTS_FILE
    if source is None:
        source = Contract.find()
    if source is None or not source.is_file():
        typer.echo(
            f"No {CONTRACTS_FILE} found here or above.\nCreate one with:\n    jfast contracts init",
        )
        raise typer.Exit(Code.CONFIG)
    return Contract.load(source), source.parent


def _row(label: str, value: str) -> str:
    """One `label   value` line, with continuation lines under the value."""
    body = textwrap.wrap(value, width=_WIDTH - _LABEL) or [""]
    head = f"  {label:<{_LABEL - 2}}{body[0]}"
    tail = [f"  {'':<{_LABEL - 2}}{line}" for line in body[1:]]
    return "\n".join([head, *tail])


def _paragraphs(label: str, values: tuple[str, ...]) -> list[str]:
    rows: list[str] = []
    for index, value in enumerate(values):
        for part in value.split("\n"):
            rows.append(_row(label if index == 0 and not rows else "", part))
    return rows


def render_explanation(answer: Explanation) -> str:
    lines = [f"{answer.question}  [{answer.verdict.upper()}]", ""]

    if answer.rule:
        # The rule summary describes a violation. Under a per-file answer,
        # where nothing was violated, it reads as an accusation about the file.
        detail = "" if answer.context.get("file") else f"  -- {RULES[answer.rule].summary}"
        lines.append(_row("rule", f"{answer.rule}{detail}"))
    if answer.summary:
        lines.append(_row("what", answer.summary))

    for declaration in answer.declarations:
        if not declaration.line:
            continue
        lines.append(_row("declared", declaration.where))
        lines.append(_row("", declaration.text))

    lines.extend(_paragraphs("why", answer.why))
    lines.extend(_paragraphs("instead", tuple(f"- {step}" for step in answer.instead)))
    lines.extend(_paragraphs("unknown", tuple(f"- {note}" for note in answer.unknown)))

    catalogue = answer.context.get("rules")
    if isinstance(catalogue, list):
        lines.append("")
        for entry in catalogue:
            at = entry["declared_at"]
            # A rule declared once per layer lists six places and wraps into a
            # wall. The full list is in --json, which is where it is read.
            shown = ", ".join(at[:3]) + (f" (+{len(at) - 3} more)" if len(at) > 3 else "")
            where = shown or "not declared here"
            lines.append(f"  {entry['rule']:<17}{where}")
            lines.append(f"  {'':<17}{entry['summary']}")

    violations = answer.context.get("violations")
    if violations:
        lines.append("")
        lines.append("  violations here now:")
        for violation in violations:
            lines.append(f"    {violation['path']}:{violation['line']}: {violation['message']}")

    lines.append("")
    lines.append(_row("waiver", f"{WAIVER_COST['inline']} -- {WAIVER_COST['scope']}"))
    lines.append(_row("", WAIVER_COST["listed_by"]))
    lines.append(_row("", WAIVER_COST["editing_the_contract"]))
    return "\n".join(lines)


def render_diff(report: ArchitectureDiff) -> str:
    lines = [f"Architecture changes  ({report.project})", ""]

    if not report.added and not report.removed:
        lines.append("  none: the code imports exactly what the contract permits")
    for delta in report.added:
        where = delta.evidence[0] if delta.evidence else ""
        lines.append(f"  + {delta.source} -> {delta.target}".ljust(34) + f"{delta.rule}  {where}")
        lines.append(f"{'':<34}{delta.reason}")
    for delta in report.removed:
        at = f"declared at {delta.declaration.where}" if delta.declaration else ""
        lines.append(f"  - {delta.source} -> {delta.target}".ljust(34) + f"{delta.reason}  {at}")

    if report.costs:
        lines.append("")
        lines.append("Potential breaking change:")
        for cost in report.costs:
            lines.append(
                textwrap.fill(cost, width=_WIDTH, initial_indent="  ", subsequent_indent="    ")
            )

    lines.append("")
    lines.append(textwrap.fill(f"Compares {report.compares}", width=_WIDTH))
    for limit in report.limits:
        lines.append(
            textwrap.fill(limit, width=_WIDTH, initial_indent="  ", subsequent_indent="  ")
        )
    return "\n".join(lines)


def _emit(payload: dict[str, Any], as_json: bool, human: str) -> None:
    typer.echo(jsonlib.dumps(payload, indent=2, default=str) if as_json else human)


def explain_command(
    subject: Annotated[
        list[str] | None,
        typer.Argument(
            help="Two names: may the first import the second? Layers, modules or a package.",
        ),
    ] = None,
    rule: Annotated[
        str | None,
        typer.Option("--rule", help=f"A rule `contracts check` reported: {', '.join(RULES)}."),
    ] = None,
    file: Annotated[
        str | None,
        typer.Option("--file", help="A source file: which layer it is in and what it may do."),
    ] = None,
    contract_path: Annotated[
        Path | None,
        typer.Option("--contract", help="contracts.toml, or the directory holding it."),
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Why a rule exists, where it is declared, and what to do instead.

    `contracts check` says a rule was broken. This says which line declares it
    and what its author was protecting, so the fix is the design change rather
    than whatever silences the checker fastest.

        jfast contracts explain billing analytics
        jfast contracts explain --rule shared-direction
        jfast contracts explain --file modules/billing/service.py
        jfast contracts explain --json

    Exits non-zero when the question cannot be answered, so a script can tell
    "no" from "I do not know".
    """
    contract, root = _require_contract(contract_path)
    answer = explain(contract, root, subject=subject or (), rule=rule, file=file)
    _emit(answer.describe(), json_out, render_explanation(answer))
    if answer.verdict == "unknown":
        raise typer.Exit(Code.USAGE)


def diff_command(
    contract_path: Annotated[
        Path | None,
        typer.Option("--contract", help="contracts.toml, or the directory holding it."),
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """The architecture the contract permits, against the one the code built.

    Not a git diff -- no revision is read, and nothing here knows what the code
    looked like yesterday. `+` is an edge the code has and the contract does
    not permit; `-` is a permission no import uses. Reporting only: the build
    is failed by `contracts check`, not by this.
    """
    contract, root = _require_contract(contract_path)
    report = diff(contract, root)
    _emit(report.describe(), json_out, render_diff(report))
