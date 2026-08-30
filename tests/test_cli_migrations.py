"""What `jfast migration check` and `jfast migration plan` must catch.

Every case here is a migration that ran, or would have run, against a real
database. The revisions are parsed from source text rather than written to a
package and imported, for the same reason the command parses them: a revision
imports the project's models, and in the CLI's environment those imports fail
long before the interesting part of the file is read.

The two commands are driven through a `typer.Typer` this module builds itself.
`register(app)` is the whole public surface, so the tests need nothing from
`main.py` -- which is also what makes the command safe to wire in last.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from jfastframework.cli import migrations
from jfastframework.cli.exits import Code

runner = CliRunner()


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
    assert _codes(source) == ["migration-timestamptz-no-using"]


def test_raw_sql_with_the_using_clause_is_clean() -> None:
    source = _revision(
        '    op.execute("ALTER TABLE invoices ALTER COLUMN created_at TYPE timestamptz '
        "USING created_at AT TIME ZONE 'UTC'\")\n"
    )
    assert _codes(source) == []


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
