"""The autogenerate hooks the generated `migrations/env.py` installs.

Both defects here are about what a revision does *not* say. Autogenerate
renders a new NOT NULL column with no default, which PostgreSQL refuses on any
table that already has rows; and it renders a rename as a drop plus an add,
which is silent data loss. The first is repairable from the model, the second
is not -- so one is fixed and the other is announced, and only when it applies.

The hooks are exercised as the template actually writes them: the rendered
``env.py`` is parsed and its functions executed in isolation, so a test cannot
pass against a template that no longer contains them.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.autogenerate import render_python_code
from alembic.operations import ops

from jfastframework.cli.scaffold import Scaffolder

TEMPLATES = Path(Scaffolder().template_root) / "service_base" / "migrations"


def _env_namespace() -> dict[str, Any]:
    """Execute only the imports and function definitions of the rendered env.py.

    The module's own body connects to a database and runs migrations, so it
    cannot simply be imported. Taking the definitions keeps the test against
    the real template rather than a copy of it that can drift.
    """
    source = (
        Scaffolder()
        .env.get_template("service_base/migrations/env.py.j2")
        .render(service_title="Test")
    )
    tree = ast.parse(source)

    def wanted(node: ast.stmt) -> bool:
        if isinstance(node, ast.Import | ast.ImportFrom | ast.FunctionDef):
            return True
        # `config = context.config` and friends reach for a live Alembic run.
        # `_HINTS` is the one assignment the hooks need.
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        return any(isinstance(t, ast.Name) and t.id == "_HINTS" for t in targets)

    kept = [node for node in tree.body if wanted(node)]
    namespace: dict[str, Any] = {"__name__": "generated_env"}
    exec(compile(ast.Module(body=kept, type_ignores=[]), "env.py", "exec"), namespace)  # nosec B102
    return namespace


def _run(directives_ops: list[Any]) -> tuple[ops.UpgradeOps, dict[str, Any]]:
    namespace = _env_namespace()
    hook = namespace["_process_revision_directives"]
    upgrade = ops.UpgradeOps(ops=directives_ops)
    script = ops.MigrationScript(
        rev_id="deadbeef", upgrade_ops=upgrade, downgrade_ops=ops.DowngradeOps(ops=[])
    )
    hook(None, (), [script])
    return upgrade, namespace["_HINTS"]


def _add(table: str, column: sa.Column[Any]) -> ops.ModifyTableOps:
    return ops.ModifyTableOps(table, [ops.AddColumnOp(table, column)])


# -- a NOT NULL column on a populated table --------------------------------


def test_a_not_null_column_with_a_scalar_default_gets_a_server_default() -> None:
    # ALTER TABLE ... ADD COLUMN status VARCHAR(20) NOT NULL is rejected
    # outright by PostgreSQL when the table has rows. The model knows the
    # answer -- default="pending" -- and Alembic only looks at server_default.
    column = sa.Column("status", sa.String(20), nullable=False, default="pending")
    upgrade, _ = _run([_add("widgets", column)])

    rendered = render_python_code(upgrade)
    assert "server_default=" in rendered, rendered
    assert "pending" in rendered, rendered


def test_the_server_default_is_dropped_again_so_the_model_stays_authoritative() -> None:
    # Leaving it behind makes the database disagree with the model, and the
    # next autogenerate would propose removing it -- churn forever.
    column = sa.Column("status", sa.String(20), nullable=False, default="pending")
    upgrade, _ = _run([_add("widgets", column)])

    rendered = render_python_code(upgrade)
    assert "alter_column" in rendered, rendered
    assert "server_default=None" in rendered, rendered


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        (sa.Column("n", sa.Integer(), nullable=False, default=0), "0"),
        (sa.Column("ok", sa.Boolean(), nullable=False, default=True), "true"),
        (sa.Column("s", sa.String(8), nullable=False, default="a'b"), "'a''b'"),
    ],
)
def test_scalar_defaults_are_rendered_as_sql_literals(
    column: sa.Column[Any], expected: str
) -> None:
    upgrade, _ = _run([_add("t", column)])
    assert expected in render_python_code(upgrade)


def test_a_nullable_column_is_left_alone() -> None:
    column = sa.Column("note", sa.String(20), nullable=True, default="x")
    upgrade, _ = _run([_add("widgets", column)])
    assert "server_default" not in render_python_code(upgrade)


def test_a_column_that_already_has_a_server_default_is_left_alone() -> None:
    column = sa.Column("n", sa.Integer(), nullable=False, server_default=sa.text("7"))
    upgrade, _ = _run([_add("widgets", column)])
    assert render_python_code(upgrade).count("server_default") == 1


def test_a_callable_default_cannot_be_repaired_and_is_announced_instead() -> None:
    # uuid4 has no SQL equivalent to backfill with, so the revision has to say
    # so rather than pretend. Silence here is what produced the failed deploy.
    column = sa.Column("uid", sa.String(36), nullable=False, default=lambda: "x")
    upgrade, hints = _run([_add("widgets", column)])

    assert "server_default" not in render_python_code(upgrade)
    assert any("widgets.uid" in entry for entry in hints["unsafe"]), hints


def test_a_not_null_column_with_no_default_at_all_is_announced() -> None:
    column = sa.Column("code", sa.String(8), nullable=False)
    _, hints = _run([_add("widgets", column)])
    assert any("widgets.code" in entry for entry in hints["unsafe"]), hints


# -- the rename warning ----------------------------------------------------


def test_a_drop_and_an_add_on_the_same_table_raise_the_rename_warning() -> None:
    table = ops.ModifyTableOps(
        "widgets",
        [
            ops.AddColumnOp("widgets", sa.Column("label", sa.String(20), nullable=True)),
            ops.DropColumnOp("widgets", "name"),
        ],
    )
    _, hints = _run([table])
    assert "widgets" in hints["renames"], hints


def test_an_add_on_its_own_does_not() -> None:
    column = sa.Column("label", sa.String(20), nullable=True)
    _, hints = _run([_add("widgets", column)])
    assert hints["renames"] == [], hints


def test_an_add_and_a_drop_on_different_tables_do_not() -> None:
    _, hints = _run(
        [
            _add("widgets", sa.Column("label", sa.String(20), nullable=True)),
            ops.ModifyTableOps("gadgets", [ops.DropColumnOp("gadgets", "name")]),
        ]
    )
    assert hints["renames"] == [], hints


def test_the_revision_template_no_longer_warns_unconditionally() -> None:
    # The warning was in every revision ever generated, which is why the person
    # who hit the bug had it on screen and did not read it.
    template = (TEMPLATES / "script.py.mako.j2").read_text(encoding="utf-8")
    docstring, _, _ = template.partition('"""\n\nfrom __future__')
    assert "rename" not in docstring.lower(), (
        "the unconditional rename warning is still in every revision's docstring"
    )
    assert "% if" in template, "nothing in the template is conditional"


# -- a custom column type has to bring its import -------------------------


class _FakeAutogenContext:
    """Only the attribute the hook writes to.

    Alembic's real AutogenContext needs a connection and a migration context;
    the hook touches one set on it and nothing else.
    """

    def __init__(self) -> None:
        self.imports: set[str] = set()


def _render(obj: Any, item_type: str = "type") -> set[str]:
    hook = _env_namespace()["_render_item"]
    context = _FakeAutogenContext()
    assert hook(item_type, obj, context) is False, "the hook must defer to Alembic's rendering"
    return context.imports


def test_a_custom_column_type_brings_its_import_with_it() -> None:
    # Alembic renders a user-defined type by its full dotted path and adds no
    # import. The revision still parses, because the name is only evaluated
    # inside upgrade() -- so it reads as correct, review passes, and it dies at
    # `alembic upgrade head` with a NameError on a line that looks fine.
    #
    # TimestampMixin uses UTCDateTime, and every module template uses the
    # mixin, so without this the FIRST migration of every generated service
    # fails.
    from jfastframework.db.base import UTCDateTime

    assert "import jfastframework.db.base" in _render(UTCDateTime())


def test_a_sqlalchemy_type_does_not_get_a_redundant_import() -> None:
    # The script template already imports `sqlalchemy as sa`; adding
    # `import sqlalchemy` beside it would be noise in every revision.
    assert _render(sa.String(20)) == set()


def test_the_timestamp_columns_of_a_real_model_are_covered() -> None:
    """The mixin as a model actually uses it, not the type in isolation."""
    from jfastframework.db.base import TimestampMixin

    registry = sa.orm.registry()

    class _Row(TimestampMixin, registry.generate_base()):  # type: ignore[misc, valid-type]
        __tablename__ = "rows"
        id: Any = sa.Column(sa.Integer, primary_key=True)

    imports: set[str] = set()
    for column in _Row.__table__.columns:
        imports |= _render(column.type)
    assert "import jfastframework.db.base" in imports


def test_only_the_type_item_is_inspected() -> None:
    # The hook is registered for every item Alembic renders. Reacting to a
    # server_default or a column would add imports for things that are not
    # types and are already rendered by name.
    from jfastframework.db.base import UTCDateTime

    assert _render(UTCDateTime(), item_type="server_default") == set()
