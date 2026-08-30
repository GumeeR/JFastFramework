"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Autogenerate is a draft, not a plan: it does not write data migrations, and it
cannot see intent. Read it before applying it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
% if jfast and jfast["renames"]:
    # -----------------------------------------------------------------
    # STOP -- as generated. This revision drops a column and adds one on
    # the same table:
    #     ${", ".join(jfast["renames"])}
    # If that pair is a rename, this migration is not one. Autogenerate
    # cannot see a rename, so it renders one as a drop plus an add --
    # which discards every value in the old column and leaves the new one
    # empty. On a table with rows, applying it as written is the loss.
    #
    # If it is a rename, replace both operations with:
    #     op.alter_column("<table>", "<old>", new_column_name="<new>")
    #
    # Or keep both and copy the values between them, which is what you
    # want when the type changes too:
    #     op.execute("UPDATE <table> SET <new> = <old>")
    #
    # This note speaks for the moment the file was generated and nothing
    # updates it afterwards. `jfast migration check` re-reads the file and
    # stays quiet once a copy sits between the add and the drop -- so when
    # it passes and this banner is still here, the banner is the stale one.
    # Delete it then.
    # -----------------------------------------------------------------
% endif
% if jfast and jfast["unsafe"]:
    # -----------------------------------------------------------------
    # NOT NULL with nothing to backfill:
    #     ${", ".join(jfast["unsafe"])}
    # Added NOT NULL with no usable default: either the model's default is
    # a callable, which has no SQL equivalent, or there is no default at
    # all. PostgreSQL rejects the statement outright once the table has
    # rows -- a scalar default would have been handled for you here.
    #
    # Add the column nullable, backfill it in this migration, then alter
    # it to NOT NULL.
    # -----------------------------------------------------------------
% endif
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
