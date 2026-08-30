"""`jfast ai context` and `jfast next`.

Two commands aimed at a reader with a context window rather than eyes, so the
tests hold them to the two things that decide whether such a command is worth
calling twice: the answer has to be *small*, and the order of the steps has to
be the order you can actually do them in.

Every fixture builds a project on disk. The generated one is built with the
real scaffolder rather than a hand-written imitation -- a fixture that drifts
from what `jfast new service` writes tests the fixture.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from jfastframework.cli import ai
from jfastframework.cli import modules as module_registry
from jfastframework.cli.exits import Code
from jfastframework.cli.patcher import insert_at_marker
from jfastframework.cli.scaffold import (
    Scaffolder,
    module_context,
    module_trees,
    service_context,
    service_trees,
)

runner = CliRunner()


@pytest.fixture
def app() -> typer.Typer:
    """A Typer of our own: `register` is the whole contract with main.py."""
    application = typer.Typer()
    ai.register(application)
    return application


# ---------------------------------------------------------------------------
# Projects to read
# ---------------------------------------------------------------------------


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def generate(root: Path, *, module: str = "invoice") -> Path:
    """What `jfast new service shop && jfast new module <module>` leaves on disk."""
    scaffolder = Scaffolder()
    scaffolder.render_trees(
        service_trees("api", None, root),
        service_context("shop", kind="api", port=8000, plugins=["database"]),
    )
    scaffolder.render_trees(
        module_trees("layered", "api", root / "modules", root),
        module_context(module, layout="layered", ui="api"),
    )
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
    module_registry.record(root, module, layout="layered", ui="api")
    return root


@pytest.fixture
def generated(tmp_path: Path) -> Path:
    return generate(tmp_path / "shop")


@pytest.fixture
def three_modules(tmp_path: Path) -> Path:
    root = generate(tmp_path / "shop")
    scaffolder = Scaffolder()
    for name in ("order", "payment"):
        scaffolder.render_trees(
            module_trees("layered", "api", root / "modules", root),
            module_context(name, layout="layered", ui="api"),
        )
        insert_at_marker(
            root / "main.py",
            "jfast:imports",
            f"from modules.{name} import router as {name}_router",
            guard=f"from modules.{name} import router as {name}_router",
            indent="",
        )
        insert_at_marker(
            root / "main.py",
            "jfast:routers",
            f"{name}_router,",
            guard=f"    {name}_router,",
            indent="    ",
        )
        module_registry.record(root, name, layout="layered", ui="api")
    return root


MINIMAL_CONFIG = """\
[app]
name = "shop"
version = "0.1.0"
env = "local"

[plugins]
enabled = []
disabled = []
"""

CLEAN_CONTRACT = """\
[project]
name = "shop"
owns = "Invoices."
does_not_own = "Customers. The catalog service owns those."
"""


def module_at(root: Path, name: str, *, table: str, tests: bool, readme: bool) -> None:
    write(root / "modules" / name / "__init__.py", "from .router import router\n")
    write(
        root / "modules" / name / "router.py",
        f'from fastapi import APIRouter\n\nrouter = APIRouter(prefix="/{table}")\n',
    )
    write(
        root / "modules" / name / "models.py",
        f'class Row:\n    __tablename__ = "{table}"\n',
    )
    if tests:
        write(root / "modules" / name / "tests" / "test_it.py", "def test_x() -> None:\n    pass\n")
    if readme:
        write(root / "modules" / name / "README.md", f"# {name}\n")


@pytest.fixture
def clean(tmp_path: Path) -> Path:
    """A project with nothing outstanding. `next` must not invent work here."""
    write(tmp_path / "jfast.toml", MINIMAL_CONFIG)
    write(
        tmp_path / "main.py",
        "from modules.invoice import router as invoice_router\n\nROUTERS = [invoice_router]\n",
    )
    write(tmp_path / "contracts.toml", CLEAN_CONTRACT)
    write(tmp_path / "modules" / "__init__.py", "")
    module_at(tmp_path, "invoice", table="invoices", tests=True, readme=True)
    write(
        tmp_path / "migrations" / "versions" / "0001_init.py",
        'def upgrade() -> None:\n    op.create_table("invoices")\n',
    )
    return tmp_path


@pytest.fixture
def drifted(tmp_path: Path) -> Path:
    """One module wired, one not, neither migrated, one untested."""
    write(tmp_path / "jfast.toml", MINIMAL_CONFIG)
    write(
        tmp_path / "main.py",
        "from modules.order import router as order_router\n\nROUTERS = [order_router]\n",
    )
    write(tmp_path / "contracts.toml", CLEAN_CONTRACT)
    write(tmp_path / "modules" / "__init__.py", "")
    module_at(tmp_path, "ghost", table="ghosts", tests=True, readme=True)
    module_at(tmp_path, "order", table="orders", tests=False, readme=True)
    # A loose file at the root: `code-outside-module`, the lowest severity
    # `analyze` emits, and the one that has to be done before the migrations.
    write(tmp_path / "helpers.py", "VALUE = 1\n")
    write(
        tmp_path / "migrations" / "versions" / "0001_init.py",
        'def upgrade() -> None:\n    op.create_table("nothing")\n',
    )
    return tmp_path


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def test_register_adds_both_commands(app: typer.Typer) -> None:
    assert runner.invoke(app, ["next", "--help"]).exit_code == 0
    assert runner.invoke(app, ["ai", "context", "--help"]).exit_code == 0


# ---------------------------------------------------------------------------
# next: the order is the point
# ---------------------------------------------------------------------------


def steps_of(app: typer.Typer, root: Path) -> list[dict[str, object]]:
    result = runner.invoke(app, ["next", "--path", str(root), "--json"])
    assert result.exit_code == 0, result.output
    payload = jsonlib.loads(result.stdout)
    return list(payload["steps"])


def test_wiring_a_module_comes_before_testing_it(app: typer.Typer, drifted: Path) -> None:
    # Testing a module main.py never imports proves nothing: the tests pass and
    # the route 404s. So the wiring step has to come first, even though the
    # missing test and the missing wiring are separate findings.
    stages = [step["stage"] for step in steps_of(app, drifted)]
    assert "wire" in stages and "cover" in stages
    assert stages.index("wire") < stages.index("cover")


def test_a_low_severity_move_comes_before_a_medium_severity_migration(
    app: typer.Typer, drifted: Path
) -> None:
    # The proof that the order is dependency and not severity. `helpers.py`
    # belonging to no module is `low`; a table with no revision is `medium`.
    # The loose file still goes first: moving it changes which tables the
    # project declares, so a revision generated before the move is regenerated
    # after it.
    steps = steps_of(app, drifted)
    stages = [step["stage"] for step in steps]
    assert stages.index("shape") < stages.index("persist")
    assert any("helpers.py" in str(step["what"]) for step in steps)


def test_the_steps_are_emitted_in_stage_order(app: typer.Typer, drifted: Path) -> None:
    ranks = [step["rank"] for step in steps_of(app, drifted)]
    assert ranks == sorted(ranks)


def test_every_step_names_a_command_or_an_edit(app: typer.Typer, drifted: Path) -> None:
    for step in steps_of(app, drifted):
        assert step["do"], f"step {step['what']!r} says what is wrong and not what to do"


def test_the_unwired_module_is_named_with_its_path(app: typer.Typer, drifted: Path) -> None:
    wire = [s for s in steps_of(app, drifted) if s["stage"] == "wire"]
    assert len(wire) == 1
    assert "ghost" in str(wire[0]["what"])
    assert "main.py" in str(wire[0]["do"])


def test_a_clean_project_is_told_it_is_done(app: typer.Typer, clean: Path) -> None:
    result = runner.invoke(app, ["next", "--path", str(clean)])
    assert result.exit_code == 0, result.output
    assert "nothing outstanding" in result.output
    assert steps_of(app, clean) == []


def test_a_clean_project_reports_ok_in_json(app: typer.Typer, clean: Path) -> None:
    result = runner.invoke(app, ["next", "--path", str(clean), "--json"])
    payload = jsonlib.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["steps"] == []


# ---------------------------------------------------------------------------
# next, on what the generator actually writes
# ---------------------------------------------------------------------------


def test_a_freshly_generated_project_is_told_to_create_the_first_revision(
    app: typer.Typer, generated: Path
) -> None:
    # `analyze` deliberately stays silent about migrations when there are none
    # at all, so it does not greet every new project with a finding. `next` is
    # the command whose entire job is the step after the one you just did.
    steps = steps_of(app, generated)
    persist = [s for s in steps if s["stage"] == "persist"]
    assert persist, f"nothing about the table on a project with no revisions: {steps}"
    assert "invoices" in str(persist[0]["what"])
    assert "alembic revision --autogenerate" in str(persist[0]["do"])


def test_a_freshly_generated_project_is_not_told_to_wire_a_module_it_wired(
    app: typer.Typer, generated: Path
) -> None:
    # The generator splices the router into main.py itself. Claiming otherwise
    # is the failure mode that makes people stop reading the output.
    assert [s for s in steps_of(app, generated) if s["stage"] == "wire"] == []


def test_a_freshly_generated_project_is_not_told_to_write_tests_it_has(
    app: typer.Typer, generated: Path
) -> None:
    assert [s for s in steps_of(app, generated) if s["stage"] == "cover"] == []


def test_the_generated_contract_placeholders_are_the_last_step(
    app: typer.Typer, generated: Path
) -> None:
    steps = steps_of(app, generated)
    document = [s for s in steps if s["stage"] == "document"]
    assert document, "the generated contracts.toml is all TODO and nothing said so"
    assert steps.index(document[0]) > 0


# ---------------------------------------------------------------------------
# ai context: what it carries
# ---------------------------------------------------------------------------


def context_of(app: typer.Typer, root: Path, *flags: str) -> dict[str, object]:
    result = runner.invoke(app, ["ai", "context", "--path", str(root), *flags])
    assert result.exit_code == 0, result.output
    return dict(jsonlib.loads(result.stdout))


def test_context_answers_in_one_call(app: typer.Typer, three_modules: Path) -> None:
    payload = context_of(app, three_modules)
    for key in (
        "schema_version",
        "project",
        "plugins",
        "modules",
        "graph",
        "contract",
        "checks",
        "next",
        "commands",
        "omitted",
    ):
        assert key in payload, f"no {key!r} in ai context"


def test_context_carries_the_schema_version_project_py_uses(
    app: typer.Typer, three_modules: Path
) -> None:
    from jfastframework.project import SCHEMA_VERSION

    assert context_of(app, three_modules)["schema_version"] == SCHEMA_VERSION


def test_context_names_the_machine_readable_commands(app: typer.Typer, generated: Path) -> None:
    listed = {str(entry["command"]) for entry in context_of(app, generated)["commands"]}  # type: ignore[index,union-attr]
    for command in (
        "jfast inspect --json",
        "jfast analyze --json",
        "jfast graph --format json",
        "jfast contracts show --json",
        "jfast contracts check --json",
    ):
        assert command in listed, f"{command} is not offered"


def test_context_does_not_ship_the_manual(app: typer.Typer, generated: Path) -> None:
    # docs/ is not in the wheel, so a pip-installed user has none of it on
    # disk, and it is ~139k tokens besides.
    text = jsonlib.dumps(context_of(app, generated))
    assert "docs" in str(context_of(app, generated)["omitted"]), (
        "docs are dropped without saying so"
    )
    assert "## " not in text, "a documentation page leaked into the payload"


def test_context_omits_per_file_listings_until_asked(app: typer.Typer, three_modules: Path) -> None:
    for module in context_of(app, three_modules)["modules"]:  # type: ignore[union-attr]
        assert "files" not in module
        assert isinstance(module["file_count"], int)


def test_narrowing_to_one_module_lists_its_files(app: typer.Typer, three_modules: Path) -> None:
    payload = context_of(app, three_modules, "--module", "invoice")
    listed = payload["modules"]
    assert isinstance(listed, list) and len(listed) == 1
    assert listed[0]["name"] == "invoice"
    assert any(name.endswith("router.py") for name in listed[0]["files"])


def test_narrowing_to_an_unknown_module_is_a_usage_error(
    app: typer.Typer, three_modules: Path
) -> None:
    result = runner.invoke(app, ["ai", "context", "--path", str(three_modules), "--module", "nope"])
    assert result.exit_code == Code.USAGE


def test_a_directory_that_is_not_a_project_is_a_config_error(
    app: typer.Typer, tmp_path: Path
) -> None:
    for argv in (["next", "--path", str(tmp_path)], ["ai", "context", "--path", str(tmp_path)]):
        assert runner.invoke(app, argv).exit_code == Code.CONFIG


def test_context_carries_the_same_steps_as_next(app: typer.Typer, generated: Path) -> None:
    assert context_of(app, generated)["next"] == steps_of(app, generated)


# ---------------------------------------------------------------------------
# ai context: how big it is
# ---------------------------------------------------------------------------

#: The documented ceiling, in bytes, for a three-module service. A context
#: command nobody can afford to call twice is a context command nobody calls.
BUDGET = 20_000


def test_context_stays_inside_its_budget(app: typer.Typer, three_modules: Path) -> None:
    result = runner.invoke(app, ["ai", "context", "--path", str(three_modules)])
    assert len(result.stdout.encode("utf-8")) < BUDGET, (
        "ai context has outgrown its documented size"
    )


def test_brief_is_a_fraction_of_the_full_answer(app: typer.Typer, three_modules: Path) -> None:
    full = runner.invoke(app, ["ai", "context", "--path", str(three_modules)]).stdout
    brief = runner.invoke(app, ["ai", "context", "--path", str(three_modules), "--brief"]).stdout
    assert len(brief) < len(full) / 2


def test_brief_keeps_the_steps_and_the_module_list(app: typer.Typer, three_modules: Path) -> None:
    payload = context_of(app, three_modules, "--brief")
    assert len(payload["modules"]) == 3  # type: ignore[arg-type]
    assert "next" in payload
    assert payload["checks"]["contracts"]["ok"] is not None  # type: ignore[index]


def test_size_reports_bytes_and_an_estimate(app: typer.Typer, three_modules: Path) -> None:
    result = runner.invoke(app, ["ai", "context", "--path", str(three_modules), "--size"])
    assert result.exit_code == 0, result.output
    assert "bytes" in result.output
    assert "tokens" in result.output
