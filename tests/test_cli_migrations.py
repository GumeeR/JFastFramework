"""What `jfast migration check` and `jfast migration plan` must catch.

Every case here is a migration that ran, or would have run, against a real
database. The revisions are parsed from source text rather than written to a
package and imported, for the same reason the command parses them: a revision
imports the project's models, and in the CLI's environment those imports fail
long before the interesting part of the file is read.

The two commands are driven through a `typer.Typer` this module builds itself.
`register(app)` is the whole public surface, so the tests need nothing from
`main.py` -- which is also what makes the command safe to wire in last.

The revision text above `_revision` is written by hand and is therefore only
evidence about revisions somebody writes by hand. The last two sections read
revisions **this framework generated** -- Alembic through the shipped
`script.py.mako`, and Alembic's own renderer through the shipped naming
convention. Those exist because the hand-written ones cannot fail the way the
product did: they agreed with the parser about a spelling the product never
used, and the applied/unapplied filter was dead for a release.

The last section takes it all the way: `alembic upgrade head` against a real
PostgreSQL, then `check` reporting what is above that head. Skipped when no
server answers. Bring one up with::

    docker run -d --name pg -p 5499:5432 \\
      -e POSTGRES_USER=jfast -e POSTGRES_PASSWORD=jfast -e POSTGRES_DB=jfast postgres:16
"""

from __future__ import annotations

import ast
import asyncio
import json as jsonlib
import os
import re
import subprocess  # nosec B404 -- runs the alembic in this venv, on generated paths
import sys
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import typer
from alembic import command
from alembic.autogenerate import render_python_code
from alembic.config import Config
from alembic.operations import ops
from alembic.script import Script
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from jfastframework.cli import migrations
from jfastframework.cli.exits import Code
from jfastframework.cli.scaffold import Scaffolder, module_context
from jfastframework.db import Base

runner = CliRunner()

# The full CLI: this file mostly drives a minimal app via _cli(), but a test
# that has to generate a service and a module needs the real command tree.
from jfastframework.cli.main import app  # noqa: E402


def _risks(source: str, *, rows: migrations.RowCounts | None = None) -> list[migrations.Risk]:
    revision = migrations.parse_revision_source(source, path="versions/0001_test.py")
    return migrations.analyze_revision(revision, rows or migrations.RowCounts.unknown())


def _codes(source: str, *, rows: migrations.RowCounts | None = None) -> list[str]:
    return [risk.finding.code for risk in _risks(source, rows=rows)]


def _revision(body: str, *, down: str = "None", identifier: str = "0001") -> str:
    return (
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        f'revision = "{identifier}"\n'
        f"down_revision = {down}\n"
        "\n"
        "def upgrade() -> None:\n"
        f"{body}"
        "\n"
        "def downgrade() -> None:\n"
        "    op.execute('-- reversed')\n"
    )


def _project(tmp_path: Path, revisions: dict[str, str]) -> Path:
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    for name, source in revisions.items():
        (versions / name).write_text(source, encoding="utf-8")
    return tmp_path


def _cli() -> typer.Typer:
    app = typer.Typer()
    migrations.register(app)
    return app


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_a_revision_that_imports_project_models_is_still_read() -> None:
    # The reason this is `ast` and not `importlib`. `modules.widget` does not
    # exist in the CLI's environment and never will.
    source = (
        "from modules.widget.models import Widget\n"
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        'revision = "0007"\n'
        'down_revision = "0006"\n'
        "def upgrade() -> None:\n"
        '    op.drop_table("widgets")\n'
        "def downgrade() -> None:\n"
        "    pass\n"
    )
    revision = migrations.parse_revision_source(source, path="versions/0007.py")
    assert revision.revision == "0007"
    assert revision.down_revision == "0006"
    assert [operation.name for operation in revision.operations] == ["drop_table"]


def test_batch_alter_table_operations_carry_the_table_from_the_with_block() -> None:
    source = _revision(
        '    with op.batch_alter_table("widgets") as batch_op:\n'
        '        batch_op.drop_column("legacy")\n'
    )
    revision = migrations.parse_revision_source(source, path="versions/0001.py")
    assert [(op.name, op.table, op.column) for op in revision.operations] == [
        ("drop_column", "widgets", "legacy")
    ]


# ---------------------------------------------------------------------------
# A NOT NULL column on a populated table
# ---------------------------------------------------------------------------


def test_a_not_null_column_with_no_server_default_is_critical() -> None:
    source = _revision(
        '    op.add_column("widgets", sa.Column("status", sa.String(), nullable=False))\n'
    )
    risks = _risks(source)
    assert [risk.finding.code for risk in risks] == ["migration-add-not-null"]
    assert risks[0].finding.severity == "critical"
    assert "widgets.status" in risks[0].finding.message


def test_a_server_default_makes_the_same_column_applicable() -> None:
    source = _revision(
        '    op.add_column("widgets", sa.Column("status", sa.String(), nullable=False,\n'
        "                  server_default=sa.text(\"'new'\")))\n"
    )
    assert "migration-add-not-null" not in _codes(source)


def test_a_not_null_column_on_a_table_created_in_the_same_revision_is_silent() -> None:
    # An empty table cannot reject the column. Reporting it is the false
    # positive that gets the whole command muted.
    source = _revision(
        '    op.create_table("widgets", sa.Column("id", sa.Integer(), primary_key=True))\n'
        '    op.add_column("widgets", sa.Column("status", sa.String(), nullable=False))\n'
    )
    assert "migration-add-not-null" not in _codes(source)


def test_the_reason_reports_a_real_row_count_when_one_is_known() -> None:
    source = _revision(
        '    op.add_column("widgets", sa.Column("status", sa.String(), nullable=False))\n'
    )
    rows = migrations.RowCounts(known=True, counts={"widgets": 1240})
    assert _risks(source, rows=rows)[0].reason == "status is NOT NULL and widgets has 1240 rows"


def test_an_unknown_row_count_is_reported_as_unknown_not_as_empty() -> None:
    source = _revision(
        '    op.add_column("widgets", sa.Column("status", sa.String(), nullable=False))\n'
    )
    reason = _risks(source)[0].reason
    assert "unknown" in reason
    assert "treat as populated" in reason


def test_an_empty_table_is_still_reported_but_says_it_is_only_this_database() -> None:
    source = _revision(
        '    op.add_column("widgets", sa.Column("status", sa.String(), nullable=False))\n'
    )
    rows = migrations.RowCounts(known=True, counts={"widgets": 0})
    risks = _risks(source, rows=rows)
    assert [risk.finding.code for risk in risks] == ["migration-add-not-null"]
    assert "on this database" in risks[0].reason


# ---------------------------------------------------------------------------
# Data loss
# ---------------------------------------------------------------------------


def test_drop_column_names_what_is_lost() -> None:
    source = _revision('    op.drop_column("widgets", "legacy_code")\n')
    risks = _risks(source)
    assert [risk.finding.code for risk in risks] == ["migration-drop-column"]
    assert "widgets.legacy_code" in risks[0].finding.message


def test_drop_table_names_what_is_lost() -> None:
    source = _revision('    op.drop_table("widgets")\n')
    risks = _risks(source)
    assert [risk.finding.code for risk in risks] == ["migration-drop-table"]
    assert "widgets" in risks[0].finding.message


def test_drop_constraint_is_reported() -> None:
    source = _revision('    op.drop_constraint("uq_widgets_slug", "widgets")\n')
    assert _codes(source) == ["migration-drop-constraint"]


def test_an_add_and_a_drop_on_one_table_read_as_a_rename() -> None:
    source = _revision(
        '    op.add_column("widgets", sa.Column("code", sa.String(), nullable=True))\n'
        '    op.drop_column("widgets", "legacy_code")\n'
    )
    codes = _codes(source)
    assert "migration-rename" in codes
    rename = next(r for r in _risks(source) if r.finding.code == "migration-rename")
    assert rename.finding.severity == "critical"
    assert "legacy_code" in rename.finding.message
    assert "code" in rename.finding.message


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


def test_create_index_without_concurrently_is_reported() -> None:
    source = _revision('    op.create_index("ix_widgets_slug", "widgets", ["slug"])\n')
    risks = _risks(source)
    assert [risk.finding.code for risk in risks] == ["migration-index-lock"]
    assert any("autocommit_block" in step for step in risks[0].steps)


def test_create_index_with_concurrently_is_clean() -> None:
    source = _revision(
        '    op.create_index("ix_widgets_slug", "widgets", ["slug"],\n'
        "                    postgresql_concurrently=True)\n"
    )
    assert _codes(source) == []


def test_an_index_on_a_table_created_in_the_same_revision_is_silent() -> None:
    source = _revision(
        '    op.create_table("widgets", sa.Column("slug", sa.String()))\n'
        '    op.create_index("ix_widgets_slug", "widgets", ["slug"])\n'
    )
    assert _codes(source) == []


def test_a_type_change_is_reported_as_a_rewrite() -> None:
    source = _revision(
        '    op.alter_column("widgets", "amount", existing_type=sa.Integer(),\n'
        "                    type_=sa.Numeric(12, 2))\n"
    )
    assert _codes(source) == ["migration-type-change"]


def test_widening_a_varchar_is_not_a_rewrite() -> None:
    # PostgreSQL takes a longer varchar as a catalogue edit. Flagging it is a
    # false positive on the single most common harmless alteration there is.
    source = _revision(
        '    op.alter_column("widgets", "slug", existing_type=sa.String(50),\n'
        "                    type_=sa.String(200))\n"
    )
    assert _codes(source) == []


def test_setting_not_null_on_an_existing_column_is_reported() -> None:
    source = _revision(
        '    op.alter_column("widgets", "slug", existing_type=sa.String(),\n'
        "                    nullable=False)\n"
    )
    assert _codes(source) == ["migration-set-not-null"]


# ---------------------------------------------------------------------------
# The one this repository already paid for
# ---------------------------------------------------------------------------


def test_a_timestamptz_change_without_using_is_critical() -> None:
    source = _revision(
        '    op.alter_column("invoices", "created_at",\n'
        "                    existing_type=sa.DateTime(),\n"
        "                    type_=sa.DateTime(timezone=True))\n"
    )
    risks = _risks(source)
    codes = [risk.finding.code for risk in risks]
    assert "migration-timestamptz-no-using" in codes
    risk = next(r for r in risks if r.finding.code == "migration-timestamptz-no-using")
    assert risk.finding.severity == "critical"
    assert "USING" in risk.finding.why
    assert any("AT TIME ZONE 'UTC'" in step for step in risk.steps)


def test_a_timestamptz_change_with_using_is_not_reported_as_silent() -> None:
    source = _revision(
        '    op.alter_column("invoices", "created_at",\n'
        "                    existing_type=sa.DateTime(),\n"
        "                    type_=sa.DateTime(timezone=True),\n"
        "                    postgresql_using=\"created_at AT TIME ZONE 'UTC'\")\n"
    )
    codes = _codes(source)
    assert "migration-timestamptz-no-using" not in codes
    # It is still a rewrite under ACCESS EXCLUSIVE, and still worth saying.
    assert codes == ["migration-type-change"]


def test_raw_sql_that_converts_to_timestamptz_without_using_is_reported() -> None:
    source = _revision(
        '    op.execute("ALTER TABLE invoices ALTER COLUMN created_at TYPE timestamptz")\n'
    )
    # The rewrite is the same rewrite whether it is written as SQL or as
    # `alter_column`, so the raw form says both things too.
    assert _codes(source) == ["migration-timestamptz-no-using", "migration-type-change"]


def test_raw_sql_with_the_using_clause_is_not_reported_as_silent() -> None:
    source = _revision(
        '    op.execute("ALTER TABLE invoices ALTER COLUMN created_at TYPE timestamptz '
        "USING created_at AT TIME ZONE 'UTC'\")\n"
    )
    risks = _risks(source)
    assert [risk.finding.code for risk in risks] == ["migration-type-change"]
    assert risks[0].finding.severity == "medium"


# ---------------------------------------------------------------------------
# No way back
# ---------------------------------------------------------------------------


def test_an_empty_downgrade_is_reported_at_low() -> None:
    source = (
        "from alembic import op\n"
        'revision = "0001"\n'
        "down_revision = None\n"
        "def upgrade() -> None:\n"
        '    op.drop_column("widgets", "legacy")\n'
        "def downgrade() -> None:\n"
        "    pass\n"
    )
    risks = _risks(source)
    by_code = {risk.finding.code: risk.finding.severity for risk in risks}
    assert by_code["migration-no-downgrade"] == "low"


def test_a_written_downgrade_is_not_reported() -> None:
    assert "migration-no-downgrade" not in _codes(_revision('    op.drop_table("widgets")\n'))


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


def test_check_exits_migration_on_a_risky_revision(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "0001_widgets.py": _revision(
                '    op.add_column("widgets", sa.Column("status", sa.String(), nullable=False))\n'
            )
        },
    )
    result = runner.invoke(_cli(), ["migration", "check", "--path", str(root), "--no-db"])
    assert result.exit_code == Code.MIGRATION, result.output
    assert "migration-add-not-null" in result.output


def test_check_is_clean_on_a_safe_revision(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "0001_widgets.py": _revision(
                '    op.create_table("widgets", sa.Column("id", sa.Integer(), primary_key=True))\n'
            )
        },
    )
    result = runner.invoke(_cli(), ["migration", "check", "--path", str(root), "--no-db"])
    assert result.exit_code == 0, result.output


def test_check_fail_on_never_reports_and_exits_zero(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "0001_widgets.py": _revision(
                '    op.add_column("widgets", sa.Column("status", sa.String(), nullable=False))\n'
            )
        },
    )
    result = runner.invoke(
        _cli(), ["migration", "check", "--path", str(root), "--no-db", "--fail-on", "never"]
    )
    assert result.exit_code == 0, result.output
    assert "migration-add-not-null" in result.output


def test_check_json_is_machine_readable(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "0001_widgets.py": _revision(
                '    op.add_column("widgets", sa.Column("status", sa.String(), nullable=False))\n'
            )
        },
    )
    result = runner.invoke(_cli(), ["migration", "check", "--path", str(root), "--no-db", "--json"])
    payload = jsonlib.loads(result.output)
    assert payload["ok"] is False
    assert payload["database"] == "skipped"
    assert payload["revisions"][0]["revision"] == "0001"
    assert payload["revisions"][0]["findings"][0]["code"] == "migration-add-not-null"
    assert payload["counts"] == {"critical": 1}


def test_check_without_a_migrations_directory_exits_config(tmp_path: Path) -> None:
    result = runner.invoke(_cli(), ["migration", "check", "--path", str(tmp_path), "--no-db"])
    assert result.exit_code == Code.CONFIG, result.output


def test_plan_prints_the_riskiest_finding_and_the_safe_rewrite(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "0001_widgets.py": _revision(
                '    op.create_table("widgets", sa.Column("id", sa.Integer(), primary_key=True))\n'
            ),
            "0004_add_status.py": _revision(
                '    op.add_column("widgets", sa.Column("status", sa.String(), nullable=False))\n',
                down='"0001"',
                identifier="0004_add_status",
            ),
        },
    )
    result = runner.invoke(_cli(), ["migration", "plan", "--path", str(root), "--no-db"])
    assert result.exit_code == Code.MIGRATION, result.output
    assert "Migration:  0004_add_status" in result.output
    assert "Risk:       CRITICAL" in result.output
    assert "1. add the column nullable" in result.output
    assert "2. backfill it" in result.output
    assert "3. add the NOT NULL constraint" in result.output


def test_plan_without_a_database_refuses_to_assume_the_table_is_empty(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "0001_widgets.py": _revision(
                '    op.add_column("widgets", sa.Column("status", sa.String(), nullable=False))\n'
            )
        },
    )
    result = runner.invoke(_cli(), ["migration", "plan", "--path", str(root), "--no-db"])
    assert "treat as populated" in result.output


def test_plan_on_a_clean_revision_exits_zero(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "0001_widgets.py": _revision(
                '    op.create_table("widgets", sa.Column("id", sa.Integer(), primary_key=True))\n'
            )
        },
    )
    result = runner.invoke(_cli(), ["migration", "plan", "--path", str(root), "--no-db"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Ordering and scope
# ---------------------------------------------------------------------------


def test_revisions_come_back_in_dependency_order_not_filename_order(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "zzz_base.py": _revision("    pass\n", identifier="aaa"),
            "aaa_second.py": _revision("    pass\n", down='"aaa"', identifier="bbb"),
        },
    )
    assert [rev.revision for rev in migrations.load_revisions(root)] == ["aaa", "bbb"]


def test_check_all_covers_revisions_an_applied_head_would_hide(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        {
            "0001.py": _revision(
                '    op.drop_table("widgets")\n',
                identifier="0001",
            )
        },
    )
    result = runner.invoke(_cli(), ["migration", "check", "--path", str(root), "--no-db", "--all"])
    assert "migration-drop-table" in result.output


def test_register_names_the_group_and_both_commands() -> None:
    app = _cli()
    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert runner.invoke(app, ["migration", "--help"]).exit_code == 0
    assert runner.invoke(app, ["migration", "check", "--help"]).exit_code == 0
    assert runner.invoke(app, ["migration", "plan", "--help"]).exit_code == 0


@pytest.mark.parametrize("level", ["critical", "high", "medium", "low", "never"])
def test_every_fail_on_level_is_accepted(tmp_path: Path, level: str) -> None:
    root = _project(tmp_path, {"0001.py": _revision("    pass\n")})
    result = runner.invoke(
        _cli(), ["migration", "check", "--path", str(root), "--no-db", "--fail-on", level]
    )
    assert result.exit_code == 0, result.output


def test_an_unknown_fail_on_level_is_a_usage_error(tmp_path: Path) -> None:
    root = _project(tmp_path, {"0001.py": _revision("    pass\n")})
    result = runner.invoke(
        _cli(), ["migration", "check", "--path", str(root), "--no-db", "--fail-on", "catastrophic"]
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Revisions this framework generated, not ones this file wrote
# ---------------------------------------------------------------------------


def _alembic_environment(root: Path) -> Path:
    """`alembic.ini` and `migrations/` rendered from the templates that ship."""
    scaffolder = Scaffolder()
    for relative, template in (
        ("alembic.ini", "service_base/alembic.ini.j2"),
        ("migrations/env.py", "service_base/migrations/env.py.j2"),
        ("migrations/script.py.mako", "service_base/migrations/script.py.mako.j2"),
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            scaffolder.env.get_template(template).render(service_title="Generated"),
            encoding="utf-8",
        )
    (root / "migrations" / "versions").mkdir()
    return root


def _alembic_config(root: Path) -> Config:
    config = Config(str(root / "alembic.ini"))
    # `script_location = migrations` in the shipped ini is resolved against the
    # working directory, which pytest does not change.
    config.set_main_option("script_location", str(root / "migrations"))
    return config


def _revise(config: Config, message: str, **options: Any) -> str:
    """The id Alembic stamped, from a revision Alembic wrote."""
    script = command.revision(config, message=message, **options)
    assert isinstance(script, Script)
    return str(script.revision)


def test_the_shipped_template_annotates_the_ids_the_parser_has_to_read() -> None:
    # The blind spot in one line: `revision: str = "..."`, not `revision = ...`.
    rendered = Scaffolder().env.get_template("service_base/migrations/script.py.mako.j2").render()
    assert "revision: str = ${repr(up_revision)}" in rendered
    assert "down_revision: str | None = ${repr(down_revision)}" in rendered


def test_a_generated_revision_reports_the_id_alembic_stamped(tmp_path: Path) -> None:
    """Parsed straight out of what `alembic revision` wrote through the template.

    Unfixed, `revision` fell back to the filename stem -- id, underscore and
    slug -- and `down_revision` was None for every revision this framework has
    ever generated.
    """
    root = _alembic_environment(tmp_path)
    config = _alembic_config(root)
    first = _revise(config, "create widgets")
    second = _revise(config, "add status", head="head")

    parsed = {revision.revision: revision for revision in migrations.load_revisions(root)}
    assert set(parsed) == {first, second}
    assert parsed[first].down_revision is None
    assert parsed[second].down_revision == first
    assert parsed[second].message == "add status"


def test_the_applied_filter_walks_the_chain_the_template_writes(tmp_path: Path) -> None:
    # The consequence of the above, and the reason it went unnoticed: with
    # every `down_revision` None the walk stops at the head, so `check` reported
    # 5 of 5 revisions unapplied against a database that was at head.
    root = _alembic_environment(tmp_path)
    config = _alembic_config(root)
    first = _revise(config, "create widgets")
    second = _revise(config, "add status", head="head")

    revisions = migrations.load_revisions(root)
    assert migrations.applied_set(revisions, second) == frozenset({first, second})
    assert migrations.applied_set(revisions, first) == frozenset({first})


def test_plan_finds_a_generated_revision_by_the_id_alembic_printed(tmp_path: Path) -> None:
    root = _alembic_environment(tmp_path)
    config = _alembic_config(root)
    _revise(config, "create widgets")
    wanted = _revise(config, "add status", head="head")

    result = runner.invoke(
        _cli(), ["migration", "plan", "--path", str(root), "--no-db", "-r", wanted]
    )
    assert result.exit_code == 0, result.output
    assert f"Migration:  {wanted}" in result.output


def test_a_generated_merge_revision_still_names_a_parent(tmp_path: Path) -> None:
    # A merge writes `down_revision: str | None = ("a", "b")`. Ordering only
    # needs one parent, but it needs the annotated form to see either.
    root = _alembic_environment(tmp_path)
    config = _alembic_config(root)
    base = _revise(config, "base")
    left = _revise(config, "left", head=base)
    right = _revise(config, "right", head=base, splice=True)
    merged = _revise(config, "merge", head=(left, right))

    parsed = {revision.revision: revision for revision in migrations.load_revisions(root)}
    assert parsed[merged].down_revision in {left, right}


# ---------------------------------------------------------------------------
# Names Alembic's renderer writes, through this framework's naming convention
# ---------------------------------------------------------------------------


def _rendered_operations() -> str:
    """Index and constraint operations as autogenerate renders them here.

    A separate `MetaData` carrying the same naming convention as `Base`: the
    convention is what turns every generated name into `op.f("...")`, and
    declaring the table on `Base` itself would leave it in the metadata every
    other test in the session shares.
    """
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    table = sa.Table(
        "widgets",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(40), index=True),
        sa.CheckConstraint("slug <> ''", name="slug_not_blank"),
    )
    # The convention is applied when the DDL is compiled, not at declaration.
    metadata.create_all(sa.create_engine("sqlite://"))
    index = next(iter(table.indexes))
    check = next(c for c in table.constraints if isinstance(c, sa.CheckConstraint))
    return render_python_code(
        ops.UpgradeOps(
            ops=[
                ops.CreateIndexOp.from_index(index),
                ops.DropIndexOp.from_index(index),
                ops.DropConstraintOp.from_constraint(check),
            ]
        )
    )


def _generated_revision_body() -> str:
    return (
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        'revision: str = "0001"\n'
        "down_revision: str | None = None\n"
        "\n"
        "def upgrade() -> None:\n"
        f"{_rendered_operations()}\n"
        "\n"
        "def downgrade() -> None:\n"
        "    pass\n"
    )


def test_a_generated_index_reports_its_own_name() -> None:
    # `op.create_index(op.f("ix_widgets_slug"), ...)`. Unfixed, every finding
    # about a generated index read "index None on widgets".
    revision = migrations.parse_revision_source(_generated_revision_body(), path="versions/0001.py")
    by_name = {operation.name: operation for operation in revision.operations}
    assert by_name["create_index"].target == "ix_widgets_slug"
    assert by_name["drop_index"].target == "ix_widgets_slug"
    assert by_name["drop_constraint"].target == "ck_widgets_slug_not_blank"


def test_a_generated_drop_index_names_a_table_the_database_can_be_asked_about() -> None:
    # `drop_index` puts the table in `table_name=`, and reading it as source
    # text yielded `'widgets'` -- quote characters included -- so the row-count
    # probe asked for a table that cannot exist and reported the real one as
    # absent.
    revision = migrations.parse_revision_source(_generated_revision_body(), path="versions/0001.py")
    assert revision.tables() == frozenset({"widgets"})


def test_a_generated_index_build_is_still_reported_as_locking() -> None:
    rows = migrations.RowCounts(known=True, counts={"widgets": 5000})
    risks = _risks(_generated_revision_body(), rows=rows)
    lock = next(risk for risk in risks if risk.finding.code == "migration-index-lock")
    assert "index ix_widgets_slug on widgets" in lock.finding.message
    assert "widgets has 5000 rows" in lock.reason


# ---------------------------------------------------------------------------
# The applied/unapplied filter against a real PostgreSQL
# ---------------------------------------------------------------------------

PG_SERVER = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
PG_DATABASE = "jfast_migration_check"

#: Alembic prints `Generating <path>/<id>_<slug>.py ... done` and nothing else
#: that carries the id. That printed id is what a user pastes into `plan -r`.
_GENERATED = re.compile(r"Generating\s+\S*[/\\](?P<id>[0-9a-f]+)_\S*\.py")


def _recreate_database(name: str = PG_DATABASE) -> bool:
    """A database of this test's own, or False when no server answers."""

    async def run() -> None:
        engine = create_async_engine(f"{PG_SERVER}/jfast", isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                await connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                await connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    except Exception:  # noqa: BLE001 - any failure means "no server here"
        return False
    return True


def _alembic(root: Path, dsn: str, *arguments: str) -> str:
    result = subprocess.run(  # nosec B603 - fixed argv, this venv's interpreter
        [sys.executable, "-m", "alembic", *arguments],
        cwd=root,
        env={**os.environ, "JFAST_DB_DSN": dsn},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{' '.join(arguments)}\n{result.stdout}\n{result.stderr}"
    return result.stdout + result.stderr


def _module(root: Path, name: str) -> Path:
    """One module, from the module template a scaffolded service gets."""
    context = module_context(name)
    package = root / "modules" / str(context["module"])
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    models = package / "models.py"
    models.write_text(
        Scaffolder().env.get_template("module_layered/{{module}}/models.py.j2").render(**context),
        encoding="utf-8",
    )
    return models


@pytest.fixture(scope="module")
def applied_service(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str, str]:
    """A service at head, plus one revision that is not applied yet.

    Everything here is generated: the module from `module_layered`, the
    environment from `service_base`, the revisions by `alembic revision
    --autogenerate` through the shipped `script.py.mako`, and the schema by
    `alembic upgrade head` against PostgreSQL. Nothing in the chain is a
    fixture written to match the parser.
    """
    if not _recreate_database():
        pytest.skip(f"no PostgreSQL at {PG_SERVER}")

    dsn = f"{PG_SERVER}/{PG_DATABASE}"
    root = _alembic_environment(tmp_path_factory.mktemp("service"))
    _module(root, "gadget")

    first = _alembic(root, dsn, "revision", "--autogenerate", "-m", "create gadgets")
    _alembic(root, dsn, "upgrade", "head")

    async def seed() -> None:
        # An empty table hides the finding this proof is about: "has rows" is
        # the difference between a migration that applies and one that stops.
        engine = create_async_engine(dsn)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text("INSERT INTO gadgets (name, is_active) VALUES ('one', true)")
                )
        finally:
            await engine.dispose()

    asyncio.run(seed())

    models = root / "modules" / "gadget" / "models.py"
    models.write_text(
        models.read_text(encoding="utf-8").replace(
            "    is_active: Mapped[bool] = mapped_column(default=True)\n",
            "    is_active: Mapped[bool] = mapped_column(default=True)\n"
            "    status: Mapped[str] = mapped_column()\n",
        ),
        encoding="utf-8",
    )
    second = _alembic(root, dsn, "revision", "--autogenerate", "-m", "add status")

    applied = _GENERATED.search(first)
    pending = _GENERATED.search(second)
    assert applied and pending, f"{first}\n{second}"
    return root, dsn, applied.group("id"), pending.group("id")


def test_check_reports_the_applied_revision_as_applied(
    applied_service: tuple[Path, str, str, str],
) -> None:
    root, dsn, applied, pending = applied_service
    result = runner.invoke(
        _cli(),
        ["migration", "check", "--path", str(root), "--dsn", dsn, "--json", "--fail-on", "never"],
    )
    payload = jsonlib.loads(result.output)
    assert payload["database"] == "connected", payload
    assert payload["scope"] == "unapplied"
    assert [report["revision"] for report in payload["revisions"]] == [pending]
    assert applied not in result.output


def test_check_all_brings_the_applied_revision_back(
    applied_service: tuple[Path, str, str, str],
) -> None:
    root, dsn, applied, pending = applied_service
    result = runner.invoke(
        _cli(),
        [
            "migration",
            "check",
            "--path",
            str(root),
            "--dsn",
            dsn,
            "--all",
            "--json",
            "--fail-on",
            "never",
        ],
    )
    payload = jsonlib.loads(result.output)
    assert payload["scope"] == "all"
    assert [report["revision"] for report in payload["revisions"]] == [applied, pending]


def test_check_reads_the_row_count_of_the_table_the_migration_touches(
    applied_service: tuple[Path, str, str, str],
) -> None:
    root, dsn, _, _ = applied_service
    result = runner.invoke(
        _cli(),
        ["migration", "check", "--path", str(root), "--dsn", dsn, "--json", "--fail-on", "never"],
    )
    payload = jsonlib.loads(result.output)
    reasons = " ".join(payload["revisions"][0]["reasons"])
    assert "gadgets" in reasons
    assert "does not exist on this database yet" not in reasons
    assert "is empty on this database" not in reasons


def test_plan_finds_the_pending_revision_by_the_id_alembic_printed(
    applied_service: tuple[Path, str, str, str],
) -> None:
    root, dsn, _, pending = applied_service
    result = runner.invoke(
        _cli(), ["migration", "plan", "--path", str(root), "--dsn", dsn, "-r", pending, "--json"]
    )
    payload = jsonlib.loads(result.output)
    assert payload["revision"] == pending
    assert payload["database"] == "connected"


# ---------------------------------------------------------------------------
# A rename that carries its data, written the way a person writes one
# ---------------------------------------------------------------------------


def _hand_edit(root: Path, identifier: str, upgrade: str, *, downgrade: str = "    pass\n") -> Path:
    """Rewrite the bodies of a revision `alembic revision` generated.

    The path a user actually takes: generate the revision, open the file,
    write the operations. What that leaves around them -- the annotated ids,
    the template's docstring, the import block, the `STOP` comment -- is
    exactly what the parser has to survive, and a source string built in this
    file has none of it.
    """
    path = next((root / "migrations" / "versions").glob(f"{identifier}_*.py"))
    source = path.read_text(encoding="utf-8")
    head, marker, tail = source.partition("def upgrade() -> None:\n")
    assert marker, source
    _, marker, _ = tail.partition("def downgrade() -> None:\n")
    assert marker, source
    path.write_text(
        f"{head}def upgrade() -> None:\n{upgrade}\n\ndef downgrade() -> None:\n{downgrade}",
        encoding="utf-8",
    )
    return path


_RENAME_BACKFILLED = (
    '    op.add_column("posts", sa.Column("media_url", sa.String(), nullable=True))\n'
    '    op.execute("UPDATE posts SET media_url = image_url")\n'
    '    op.drop_column("posts", "image_url")\n'
)
_RENAME_BARE = (
    '    op.add_column("posts", sa.Column("media_url", sa.String(), nullable=True))\n'
    '    op.drop_column("posts", "image_url")\n'
)
_RENAME_THREE_ADDITIONS = (
    '    op.add_column("posts", sa.Column("media_kind", sa.String(), nullable=True))\n'
    '    op.add_column("posts", sa.Column("media_url", sa.String(), nullable=True))\n'
    '    op.add_column("posts", sa.Column("reaction_counts", sa.JSON(), nullable=True))\n'
    '    op.drop_column("posts", "image_url")\n'
)


def _generated_project(tmp_path: Path, upgrade: str, *, message: str = "rename image_url") -> Path:
    root = _alembic_environment(tmp_path)
    identifier = _revise(_alembic_config(root), message)
    _hand_edit(root, identifier, upgrade, downgrade='    op.execute("-- one way")\n')
    return root


def _findings(root: Path) -> list[dict[str, Any]]:
    result = runner.invoke(
        _cli(),
        ["migration", "check", "--path", str(root), "--no-db", "--json", "--fail-on", "never"],
    )
    payload = jsonlib.loads(result.output)
    return [finding for report in payload["revisions"] for finding in report["findings"]]


def test_a_rename_that_copies_its_data_first_is_not_reported_as_a_rename(tmp_path: Path) -> None:
    # The false positive verbatim: add, `UPDATE posts SET media_url = image_url`,
    # drop. The evidence is in the file, between the two operations named.
    root = _generated_project(tmp_path, _RENAME_BACKFILLED)
    assert "migration-rename" not in [finding["code"] for finding in _findings(root)]


def test_a_rename_with_nothing_copying_the_values_is_still_critical(tmp_path: Path) -> None:
    # The inverse, and the reason the suppression above has to be narrow: the
    # same revision minus the UPDATE really does lose the column.
    root = _generated_project(tmp_path, _RENAME_BARE)
    rename = next(f for f in _findings(root) if f["code"] == "migration-rename")
    assert rename["severity"] == "critical"
    assert "image_url" in rename["message"]
    assert "media_url" in rename["message"]


def test_a_copy_placed_after_the_drop_does_not_count(tmp_path: Path) -> None:
    # It cannot: the column it reads is gone by the time the statement runs.
    root = _generated_project(
        tmp_path,
        '    op.add_column("posts", sa.Column("media_url", sa.String(), nullable=True))\n'
        '    op.drop_column("posts", "image_url")\n'
        '    op.execute("UPDATE posts SET media_url = image_url")\n',
    )
    assert "migration-rename" in [finding["code"] for finding in _findings(root)]


def test_an_update_that_names_only_one_of_the_two_columns_does_not_count(tmp_path: Path) -> None:
    root = _generated_project(
        tmp_path,
        '    op.add_column("posts", sa.Column("media_url", sa.String(), nullable=True))\n'
        "    op.execute(\"UPDATE posts SET media_url = 'unknown'\")\n"
        '    op.drop_column("posts", "image_url")\n',
    )
    assert "migration-rename" in [finding["code"] for finding in _findings(root)]


def test_the_rename_finding_points_at_the_drop_not_at_the_first_operation(tmp_path: Path) -> None:
    root = _generated_project(tmp_path, _RENAME_BARE)
    path = next((root / "migrations" / "versions").glob("*.py"))
    lines = path.read_text(encoding="utf-8").splitlines()
    drop = next(number for number, text in enumerate(lines, 1) if "op.drop_column" in text)
    rename = next(f for f in _findings(root) if f["code"] == "migration-rename")
    assert rename["line"] == drop


def test_a_correct_migration_produces_no_finding_at_all(tmp_path: Path) -> None:
    # A checker with no negative case is a checker that will cry wolf. This is
    # the whole of it: a new table, its index built without blocking writes,
    # and a downgrade that undoes both.
    root = _generated_project(
        tmp_path,
        "    op.create_table(\n"
        '        "posts",\n'
        '        sa.Column("id", sa.Integer(), primary_key=True),\n'
        '        sa.Column("slug", sa.String(64), nullable=False),\n'
        "    )\n"
        "    with op.get_context().autocommit_block():\n"
        "        op.create_index(\n"
        '            "ix_posts_slug", "posts", ["slug"], postgresql_concurrently=True\n'
        "        )\n",
        message="create posts",
    )
    assert _findings(root) == []


# ---------------------------------------------------------------------------
# Every remedy this command prints
# ---------------------------------------------------------------------------

#: One revision body per finding code, so the guard below cannot pass by
#: having nothing to check.
_ONE_PER_FINDING = {
    "migration-add-not-null": _revision(
        '    op.add_column("widgets", sa.Column("status", sa.String(), nullable=False))\n'
    ),
    "migration-drop-column": _revision('    op.drop_column("widgets", "legacy")\n'),
    "migration-drop-table": _revision('    op.drop_table("widgets")\n'),
    "migration-drop-constraint": _revision(
        '    op.drop_constraint("uq_widgets_slug", "widgets")\n'
    ),
    "migration-index-lock": _revision(
        '    op.create_index("ix_widgets_slug", "widgets", ["slug"])\n'
    ),
    "migration-set-not-null": _revision(
        '    op.alter_column("widgets", "slug", existing_type=sa.String(), nullable=False)\n'
    ),
    "migration-type-change": _revision(
        '    op.alter_column("widgets", "amount", existing_type=sa.Integer(),\n'
        "                    type_=sa.Numeric(12, 2))\n"
    ),
    "migration-timestamptz-no-using": _revision(
        '    op.alter_column("invoices", "created_at", existing_type=sa.DateTime(),\n'
        "                    type_=sa.DateTime(timezone=True))\n"
    ),
    "migration-rename": _revision(_RENAME_BARE),
    "migration-raw-sql": _revision('    op.execute("VACUUM ANALYZE widgets")\n'),
    "migration-no-downgrade": (
        "from alembic import op\n"
        'revision = "0001"\n'
        "down_revision = None\n"
        "def upgrade() -> None:\n"
        '    op.drop_column("widgets", "legacy")\n'
        "def downgrade() -> None:\n"
        "    pass\n"
    ),
}

_NAME_KEYWORDS = frozenset({"new_column_name", "table_name", "column_name"})
_NAME_ARGUMENTS = frozenset({"op.add_column", "op.alter_column", "op.drop_column"})


def _remedies(risk: migrations.Risk) -> list[str]:
    return migrations.python_remedies((*risk.steps, risk.finding.why))


@pytest.mark.parametrize("code", sorted(_ONE_PER_FINDING))
def test_every_remedy_this_command_prints_parses(code: str) -> None:
    # The cheapest guard there is against a remedy someone pastes: it has to
    # be Python before it can be right.
    risks = _risks(_ONE_PER_FINDING[code])
    assert code in [risk.finding.code for risk in risks]
    for risk in risks:
        for remedy in _remedies(risk):
            ast.parse(remedy)


@pytest.mark.parametrize("code", sorted(_ONE_PER_FINDING))
def test_no_remedy_puts_several_names_where_one_belongs(code: str) -> None:
    # `new_column_name="media_kind, media_url, reaction_counts"` parses and is
    # still nonsense. Parsing is the floor, not the bar.
    for risk in _risks(_ONE_PER_FINDING[code]):
        for remedy in _remedies(risk):
            for node in ast.walk(ast.parse(remedy)):
                if isinstance(node, ast.keyword) and node.arg in _NAME_KEYWORDS:
                    assert isinstance(node.value, ast.Constant), remedy
                    assert str(node.value.value).isidentifier(), remedy
                if isinstance(node, ast.Call) and ast.unparse(node.func) in _NAME_ARGUMENTS:
                    for argument in node.args:
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                            assert argument.value.isidentifier(), remedy


def test_a_rename_with_three_new_columns_offers_one_remedy_per_candidate(tmp_path: Path) -> None:
    # The emitted defect: three names joined into one `new_column_name`.
    root = _generated_project(tmp_path, _RENAME_THREE_ADDITIONS)
    identifier = next((root / "migrations" / "versions").glob("*.py")).name.split("_")[0]
    result = runner.invoke(
        _cli(), ["migration", "plan", "--path", str(root), "--no-db", "-r", identifier, "--json"]
    )
    payload = jsonlib.loads(result.output)
    assert payload["finding"]["code"] == "migration-rename"
    named = [
        ast.literal_eval(remedy.split("new_column_name=")[1].rstrip(")"))
        for remedy in migrations.python_remedies(payload["steps"])
        if "new_column_name=" in remedy
    ]
    assert sorted(named) == ["media_kind", "media_url", "reaction_counts"]


# ---------------------------------------------------------------------------
# Raw SQL: what is read, and what is admitted to be unread
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "code"),
    [
        ("DROP TABLE posts", "migration-drop-table"),
        ("ALTER TABLE posts DROP COLUMN image_url", "migration-drop-column"),
        ("CREATE INDEX ix_posts_slug ON posts (slug)", "migration-index-lock"),
        # PostgreSQL derives the name when it is left out; the lock does not care.
        ("CREATE INDEX ON posts (slug)", "migration-index-lock"),
        ("ALTER TABLE posts ALTER COLUMN slug SET NOT NULL", "migration-set-not-null"),
        ("TRUNCATE TABLE posts", "migration-drop-table"),
        ("ALTER TABLE posts ALTER COLUMN amount TYPE numeric(12, 2)", "migration-type-change"),
    ],
)
def test_raw_sql_that_is_dangerous_is_reported(statement: str, code: str) -> None:
    assert code in _codes(_revision(f'    op.execute("{statement}")\n'))


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE INDEX CONCURRENTLY ix_posts_slug ON posts (slug)",
        "ALTER TABLE posts ALTER COLUMN amount TYPE numeric(12, 2) USING amount::numeric",
    ],
)
def test_raw_sql_that_says_how_is_not_reported_as_blind(statement: str) -> None:
    codes = _codes(_revision(f'    op.execute("{statement}")\n'))
    assert "migration-index-lock" not in codes
    assert "migration-raw-sql" not in codes


def test_sql_this_check_does_not_read_is_said_so_rather_than_ticked() -> None:
    # A `✓` on a statement nobody parsed is the overclaim. It is reported at
    # `low`, so it states the gap without blocking a deploy.
    risks = _risks(_revision('    op.execute("VACUUM ANALYZE widgets")\n'))
    unread = next(risk for risk in risks if risk.finding.code == "migration-raw-sql")
    assert unread.finding.severity == "low"


def test_sql_built_at_runtime_is_reported_as_unread() -> None:
    source = _revision('    op.execute(f"ALTER TABLE {name} DROP COLUMN legacy")\n')
    assert "migration-raw-sql" in _codes(source)


def test_check_does_not_tick_a_revision_whose_sql_it_did_not_read(tmp_path: Path) -> None:
    root = _project(tmp_path, {"0001.py": _revision('    op.execute("VACUUM ANALYZE widgets")\n')})
    result = runner.invoke(_cli(), ["migration", "check", "--path", str(root), "--no-db"])
    assert result.exit_code == 0, result.output
    assert "migration-raw-sql" in result.output


# ---------------------------------------------------------------------------
# A type change whose author wrote the conversion
# ---------------------------------------------------------------------------


def test_a_type_change_with_postgresql_using_does_not_block_the_default_threshold() -> None:
    source = _revision(
        '    op.alter_column("invoices", "created_at",\n'
        "                    existing_type=sa.DateTime(),\n"
        "                    type_=sa.DateTime(timezone=True),\n"
        "                    postgresql_using=\"created_at AT TIME ZONE 'UTC'\")\n"
    )
    risk = next(r for r in _risks(source) if r.finding.code == "migration-type-change")
    assert risk.finding.severity == "medium"


def test_a_type_change_without_postgresql_using_still_blocks() -> None:
    source = _revision(
        '    op.alter_column("widgets", "amount", existing_type=sa.Integer(),\n'
        "                    type_=sa.Numeric(12, 2))\n"
    )
    risk = next(r for r in _risks(source) if r.finding.code == "migration-type-change")
    assert risk.finding.severity == "high"


def test_the_empty_downgrade_finding_points_at_the_downgrade() -> None:
    source = (
        "from alembic import op\n"
        'revision = "0001"\n'
        "down_revision = None\n"
        "def upgrade() -> None:\n"
        '    op.drop_column("widgets", "legacy")\n'
        "def downgrade() -> None:\n"
        "    pass\n"
    )
    risk = next(r for r in _risks(source) if r.finding.code == "migration-no-downgrade")
    assert risk.finding.line == 6


# ---------------------------------------------------------------------------
# The remedy, run against the real PostgreSQL
# ---------------------------------------------------------------------------

PG_RENAME_DATABASE = "jfast_migration_remedy"


def _run_sql(dsn: str, statement: str) -> Any:
    async def run() -> Any:
        engine = create_async_engine(dsn)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(sa.text(statement))
                return result.scalar() if result.returns_rows else None
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.fixture(scope="module")
def rename_service(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, str]:
    """`posts` at head with one row, plus an unapplied drop-plus-add rename."""
    if not _recreate_database(PG_RENAME_DATABASE):
        pytest.skip(f"no PostgreSQL at {PG_SERVER}")

    dsn = f"{PG_SERVER}/{PG_RENAME_DATABASE}"
    root = _alembic_environment(tmp_path_factory.mktemp("rename"))
    config = _alembic_config(root)

    created = _revise(config, "create posts")
    _hand_edit(
        root,
        created,
        "    op.create_table(\n"
        '        "posts",\n'
        '        sa.Column("id", sa.Integer(), primary_key=True),\n'
        '        sa.Column("image_url", sa.String()),\n'
        "    )\n",
        downgrade='    op.drop_table("posts")\n',
    )
    _alembic(root, dsn, "upgrade", "head")
    _run_sql(dsn, "INSERT INTO posts (image_url) VALUES ('before.png')")

    pending = _revise(config, "rename image url", head="head")
    _hand_edit(root, pending, _RENAME_BARE)
    return root, dsn, pending


def test_the_rename_remedy_runs_and_the_values_survive(
    rename_service: tuple[Path, str, str],
) -> None:
    # "Valid Python" is the floor. This is the bar: paste the remedy into the
    # revision, run it against PostgreSQL, and read the row back.
    root, dsn, pending = rename_service
    result = runner.invoke(
        _cli(), ["migration", "plan", "--path", str(root), "--dsn", dsn, "-r", pending, "--json"]
    )
    payload = jsonlib.loads(result.output)
    remedy = next(
        step for step in migrations.python_remedies(payload["steps"]) if "new_column_name=" in step
    )
    _hand_edit(root, pending, f"    {remedy}\n")
    _alembic(root, dsn, "upgrade", "head")
    assert _run_sql(dsn, "SELECT media_url FROM posts") == "before.png"


def test_the_backfilled_rename_applies_and_is_not_reported(
    rename_service: tuple[Path, str, str],
) -> None:
    root, dsn, _ = rename_service
    identifier = _revise(_alembic_config(root), "add media kind", head="head")
    _hand_edit(
        root,
        identifier,
        '    op.add_column("posts", sa.Column("media_kind", sa.String(), nullable=True))\n'
        '    op.execute("UPDATE posts SET media_kind = media_url")\n'
        '    op.drop_column("posts", "media_url")\n',
    )
    result = runner.invoke(
        _cli(),
        [
            "migration",
            "check",
            "--path",
            str(root),
            "--dsn",
            dsn,
            "--json",
            "--fail-on",
            "never",
        ],
    )
    payload = jsonlib.loads(result.output)
    codes = [f["code"] for report in payload["revisions"] for f in report["findings"]]
    assert "migration-rename" not in codes

    _alembic(root, dsn, "upgrade", "head")
    assert _run_sql(dsn, "SELECT media_kind FROM posts") == "before.png"


def test_a_hexagonal_module_reaches_base_metadata_without_the_package_init(tmp_path):
    """`env.py` looked for models.py and storage.py. Hexagonal writes neither:
    its tables are in `infrastructure/orm.py`, and they reached Base.metadata
    only because the module's `__init__` imports `.adapters.http`, which drags
    the ORM behind it -- while that `__init__`'s own docstring says nothing
    there should be imported at module scope.

    So the assertion has to be made with that side effect removed. With it in
    place the old code passes, and the day somebody makes the import lazy,
    autogenerate emits an empty migration and says nothing.
    """
    import sys

    target = tmp_path / "shop"
    assert (
        runner.invoke(
            app, ["new", "service", "shop", "--target", str(target), "--with", "database"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            [
                "new",
                "module",
                "billing",
                "--layout",
                "hexagonal",
                "--root",
                str(target),
                "--target",
                str(target / "modules"),
            ],
        ).exit_code
        == 0
    )

    init = target / "modules" / "billing" / "__init__.py"
    init.write_text('"""Nothing here is imported at module scope."""\n', encoding="utf-8")

    env_source = (target / "migrations" / "env.py").read_text(encoding="utf-8")
    assert "infrastructure.orm" in env_source, "env.py does not look for the ORM at all"

    # Run the importer the template ships, in this project, with a fresh
    # metadata -- not a re-implementation of it.
    namespace: dict[str, object] = {}
    start = env_source.index("def _import_module_models")
    # The call site, not the  line -- which also contains
    #  and would slice off four characters.
    end = env_source.index(chr(10) + "_import_module_models()", start)
    sys.path.insert(0, str(target))
    for name in [n for n in sys.modules if n == "modules" or n.startswith("modules.")]:
        del sys.modules[name]
    try:
        exec(
            "import importlib, pkgutil\nfrom pathlib import Path\n"
            f"__file__ = {str(target / 'migrations' / 'env.py')!r}\n" + env_source[start:end],
            namespace,
        )
        from sqlalchemy import MetaData

        from jfastframework.db import Base

        before = set(Base.metadata.tables)
        namespace["_import_module_models"]()  # type: ignore[operator]
        found = set(Base.metadata.tables) - before
    finally:
        sys.path.remove(str(target))
        for name in [n for n in sys.modules if n == "modules" or n.startswith("modules.")]:
            del sys.modules[name]

    assert any(name.startswith("billing") for name in found), (
        f"the hexagonal module's table never registered: {sorted(found)}. "
        "Autogenerate would emit an empty migration and report success."
    )
    assert MetaData  # keep the import meaningful to linters
