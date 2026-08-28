"""Declarative base with a stable constraint naming convention.

Why this matters: without an explicit ``naming_convention``, PostgreSQL invents
constraint names and Alembic's ``--autogenerate`` produces different diffs on
different machines. Pinning the convention here makes migrations reproducible
across every service built on JFast.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """created_at / updated_at maintained by the database, not the app."""

    # Fetch server-side defaults as part of the INSERT/UPDATE rather than
    # on the next attribute access. Without this, `onupdate` expires
    # `updated_at` at flush time, and the first read -- usually Pydantic
    # serialising the response -- triggers lazy IO inside a coroutine and
    # raises MissingGreenlet. PostgreSQL returns the values with RETURNING,
    # so this costs no extra round trip; `session.refresh()` would cost one
    # SELECT on every write, including the writes that never read a
    # timestamp.
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TenantMixin:
    """Tenant scoping column.

    Adding the column is the easy half. Enforcing that no query forgets the
    filter is the hard half -- ``BaseRepository`` does it for repository
    traffic, but raw ``session.execute`` calls bypass it. Row-level security
    is the phase-2 answer; see PLAN.md.
    """

    tenant_id: Mapped[str | None] = mapped_column(index=True, nullable=True)
