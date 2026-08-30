"""`jfast check`: one battery, one exit code, and no silent green.

The tests that matter here are not the ones proving a broken project fails.
They are the ones proving a check that could not run says so. A battery that
reports a check it skipped as a pass is worse than no battery: it converts "I
did not look" into "I looked and it was fine", and the first person to trust it
ships the thing it never examined.

Every fixture is a real generated service -- the same templates `jfast new
service` renders -- because a checker tuned against a hand-written fixture only
proves it agrees with the fixture.
"""

from __future__ import annotations

import inspect
import json as jsonlib
from collections.abc import Sequence
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from jfastframework.cli import check as check_cli
from jfastframework.cli.exits import Code
from jfastframework.cli.patcher import insert_at_marker
from jfastframework.cli.scaffold import (
    Scaffolder,
    module_context,
    module_trees,
    service_context,
    service_trees,
)
from jfastframework.project import Finding

runner = CliRunner()


def _cli() -> typer.Typer:
    """A Typer app carrying nothing but `check`.

    The callback is load-bearing: a Typer app with exactly one command
    collapses into that command, and `["check", ...]` would then be parsed as
    arguments rather than as the command name.
    """
    app = typer.Typer()

    @app.callback()
    def _root() -> None: ...

    check_cli.register(app)
    return app


def _mount(root: Path, module: str) -> None:
    insert_at_marker(
        root / "main.py",
        "jfast:imports",
        f"from modules.{module} import router as {module}_router",
        guard=f"from modules.{module} import router as {module}_router",
        indent="",
    )
    insert_at_marker(
        root / "main.py",
        "jfast:routers",
        f"{module}_router,",
        guard=f"    {module}_router,",
        indent="    ",
    )


def _service(
    tmp_path: Path, *, mounted: Sequence[str] = ("invoice",), loose: Sequence[str] = ()
) -> Path:
    """A generated service, optionally with modules main.py never hears about."""
    root = tmp_path / "shop"
    scaffolder = Scaffolder()
    scaffolder.render_trees(service_trees("api", None, root), service_context("shop"))
    for name in (*mounted, *loose):
        scaffolder.render_trees(
            module_trees("layered", "api", root / "modules", root),
            module_context(name),
        )
    for name in mounted:
        _mount(root, name)
    return root


def _failed(name: str) -> check_cli.CheckResult:
    """A check with one finding, for pinning the exit-code precedence."""
    return check_cli.CheckResult(
        name=name,
        findings=(Finding(severity="critical", code="x", message="m", why="w"),),
    )


def _run(root: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(_cli(), ["check", "--path", str(root), *args])
    return result.exit_code, result.stdout


def _json(root: Path, *args: str) -> tuple[int, dict]:
    code, out = _run(root, "--json", *args)
    return code, jsonlib.loads(out)


# ---------------------------------------------------------------------------
# The shape of the thing
# ---------------------------------------------------------------------------


def test_register_takes_a_typer_app_and_returns_nothing() -> None:
    signature = inspect.signature(check_cli.register)
    assert list(signature.parameters) == ["app"]
    assert signature.parameters["app"].annotation in (typer.Typer, "typer.Typer")
    assert signature.return_annotation in (None, "None", type(None))


def test_a_healthy_generated_project_passes(tmp_path: Path) -> None:
    root = _service(tmp_path)
    code, payload = _json(root)
    assert code == Code.OK, payload
    assert payload["ok"] is True
    assert payload["failed"] == []


# ---------------------------------------------------------------------------
# A skip is not a pass. The point of the whole command.
# ---------------------------------------------------------------------------


def test_a_check_that_cannot_run_is_reported_as_skipped_not_as_a_pass(tmp_path: Path) -> None:
    root = _service(tmp_path)
    (root / "contracts.toml").unlink()

    code, payload = _json(root)
    contracts = next(c for c in payload["checks"] if c["name"] == "contracts")

    assert contracts["status"] == "skip"
    assert contracts["reason"], "a skip with no reason is indistinguishable from a pass"
    assert "contracts" in payload["skipped"]
    assert "contracts" not in payload["passed"]
    assert payload["complete"] is False
    assert payload["summary"]["skip"] >= 1
    assert code == Code.OK


def test_a_skip_is_visible_in_the_human_output_too(tmp_path: Path) -> None:
    root = _service(tmp_path)
    (root / "contracts.toml").unlink()

    _, out = _run(root)
    assert "skipped" in out
    assert "contracts" in out


def test_ci_fails_on_a_skip(tmp_path: Path) -> None:
    root = _service(tmp_path)
    (root / "contracts.toml").unlink()

    code, payload = _json(root, "--ci")
    assert code == Code.ENVIRONMENT
    assert payload["ok"] is False


def test_allow_skips_is_the_only_way_past_it(tmp_path: Path) -> None:
    root = _service(tmp_path)
    (root / "contracts.toml").unlink()

    code, payload = _json(root, "--ci", "--allow-skips")
    assert code == Code.OK
    assert payload["skipped"] == ["contracts"]


def test_the_migrations_check_reports_itself_when_its_command_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sibling command may or may not be installed in this build. Either way
    # `check` must not pretend it ran.
    monkeypatch.setattr(check_cli, "_migration_findings", check_cli._unavailable)
    root = _service(tmp_path)
    _, payload = _json(root)
    migrations = next(c for c in payload["checks"] if c["name"] == "migrations")
    assert migrations["status"] == "skip"
    assert migrations["reason"]


# ---------------------------------------------------------------------------
# Findings and exit codes
# ---------------------------------------------------------------------------


def test_a_contract_violation_exits_with_the_contract_code(tmp_path: Path) -> None:
    root = _service(tmp_path)
    service = root / "modules" / "invoice" / "service.py"
    service.write_text(
        service.read_text(encoding="utf-8") + '\n\ndef shout() -> None:\n    print("x")\n',
        encoding="utf-8",
    )

    code, payload = _json(root)
    assert code == Code.CONTRACT
    assert "contracts" in payload["failed"]


def test_an_unregistered_module_exits_with_the_validation_code(tmp_path: Path) -> None:
    root = _service(tmp_path, loose=("order",))
    code, payload = _json(root)
    assert code == Code.VALIDATION
    assert "analyze" in payload["failed"]
    codes = [f["code"] for c in payload["checks"] for f in c["findings"]]
    assert "module-unregistered" in codes


def test_a_contract_violation_outranks_a_structural_one(tmp_path: Path) -> None:
    root = _service(tmp_path, loose=("order",))
    service = root / "modules" / "invoice" / "service.py"
    service.write_text(
        service.read_text(encoding="utf-8") + '\n\ndef shout() -> None:\n    print("x")\n',
        encoding="utf-8",
    )

    code, payload = _json(root)
    assert code == Code.CONTRACT
    assert set(payload["failed"]) >= {"analyze", "contracts"}
    # The single number is one of many. The payload has to carry the rest or a
    # script has to guess what else broke.
    assert sorted(payload["codes"]) == [int(Code.VALIDATION), int(Code.CONTRACT)]


@pytest.mark.parametrize(
    ("failing", "expected"),
    [
        (["config", "contracts"], Code.CONFIG),
        (["plugins", "migrations"], Code.ENVIRONMENT),
        (["migrations", "contracts"], Code.MIGRATION),
        (["contracts", "analyze"], Code.CONTRACT),
        (["analyze", "deploy"], Code.VALIDATION),
    ],
)
def test_the_precedence_is_cause_before_effect(failing: list[str], expected: Code) -> None:
    results = [_failed(name) for name in failing]
    assert check_cli.worst_code(results, fail_on="low", strict=False) == expected


def test_a_skip_under_ci_loses_to_a_real_failure() -> None:
    results = [_failed("contracts"), check_cli.skipped("migrations", "not installed")]
    assert check_cli.worst_code(results, fail_on="low", strict=True) == Code.CONTRACT


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_only_runs_just_those_checks(tmp_path: Path) -> None:
    root = _service(tmp_path)
    _, payload = _json(root, "--only", "contracts,analyze")
    assert [c["name"] for c in payload["checks"]] == ["analyze", "contracts"]


def test_only_rejects_a_name_that_is_not_a_check(tmp_path: Path) -> None:
    root = _service(tmp_path)
    code, _ = _run(root, "--only", "vibes")
    assert code == Code.USAGE


def test_a_directory_that_is_not_a_service_exits_config(tmp_path: Path) -> None:
    code, _ = _run(tmp_path)
    assert code == Code.CONFIG
