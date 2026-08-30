"""BaseRepository: ordering and tenant scoping, both of which used to fail quietly.

Runs against SQLite in memory. What is being tested is the SQL the repository
builds and the guardrails around it, not PostgreSQL behaviour -- with one
exception: the timestamp DDL is compiled against the PostgreSQL dialect,
because ``TIMESTAMP WITHOUT TIME ZONE`` is a PostgreSQL type name and SQLite
would render neither spelling.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import CreateTable

from jfastframework.db.base import TimestampMixin
from jfastframework.db.repository import BaseRepository, Page
from jfastframework.errors import NotFoundError


class Base(DeclarativeBase):
    pass


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(default="t1")
    label: Mapped[str] = mapped_column(default="")


class Currency(Base):
    """Reference data. Global on purpose, and it says so."""

    __tablename__ = "currencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(default="")


class Stamped(TimestampMixin, Base):
    """Carries the mixin and nothing else, so the timestamps are the subject."""

    __tablename__ = "stamped"

    id: Mapped[int] = mapped_column(primary_key=True)


class InvoiceRepository(BaseRepository[Invoice]):
    model = Invoice


class CurrencyRepository(BaseRepository[Currency]):
    model = Currency
    tenant_scoped = False


class LabelledInvoiceRepository(BaseRepository[Invoice]):
    model = Invoice
    order_by = ("label",)


class NewestInvoiceRepository(BaseRepository[Invoice]):
    model = Invoice
    order_by = ("-label",)


class StampedRepository(BaseRepository[Stamped]):
    model = Stamped
    tenant_scoped = False


@pytest.fixture
async def session():  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as open_session:
        yield open_session
    await engine.dispose()


@pytest.fixture
async def recorded():  # type: ignore[no-untyped-def]
    """A session plus every statement its engine sent to the driver.

    Asserting "no COUNT was issued" needs the SQL, not the result: a mode that
    counts and then discards the number looks identical from the outside.
    """
    statements: list[str] = []
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(statement)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as open_session:
        yield open_session, statements
    await engine.dispose()


# -- tenant scoping ----------------------------------------------------


def test_a_tenant_id_on_a_model_without_the_column_raises() -> None:
    """The silent no-op this replaces returned every tenant's rows."""

    class Wrong(BaseRepository[Currency]):
        model = Currency

    with pytest.raises(TypeError, match="has no tenant_id column"):
        Wrong(session=None, tenant_id="t1")  # type: ignore[arg-type]


def test_global_models_opt_out_explicitly() -> None:
    CurrencyRepository(session=None, tenant_id="t1")  # type: ignore[arg-type]


async def test_queries_are_filtered_by_tenant(session) -> None:  # type: ignore[no-untyped-def]
    mine = InvoiceRepository(session, tenant_id="t1")
    await mine.create(label="a")
    theirs = InvoiceRepository(session, tenant_id="t2")
    await theirs.create(label="b")

    assert [i.label for i in await mine.find()] == ["a"]
    assert await mine.count() == 1


async def test_create_stamps_the_tenant(session) -> None:  # type: ignore[no-untyped-def]
    repository = InvoiceRepository(session, tenant_id="t9")
    invoice = await repository.create(label="x")
    assert invoice.tenant_id == "t9"


# -- ordering ----------------------------------------------------------


async def test_pages_do_not_repeat_or_skip_rows(session) -> None:  # type: ignore[no-untyped-def]
    repository = InvoiceRepository(session)
    for index in range(10):
        await repository.create(label=f"row-{index:02d}")

    first = await repository.paginate(limit=4, offset=0)
    second = await repository.paginate(limit=4, offset=4)
    third = await repository.paginate(limit=4, offset=8)

    seen = [i.id for i in first.items + second.items + third.items]
    assert seen == sorted(seen), "pages came back out of order"
    assert len(set(seen)) == 10, "a row appeared on two pages"
    assert first.total == 10
    assert first.has_more is True
    assert third.has_more is False


async def test_order_by_is_overridable(session) -> None:  # type: ignore[no-untyped-def]
    repository = LabelledInvoiceRepository(session)
    await repository.create(label="c")
    await repository.create(label="a")
    await repository.create(label="b")

    assert [i.label for i in await repository.find()] == ["a", "b", "c"]


async def test_paginate_counts_before_it_orders(session) -> None:  # type: ignore[no-untyped-def]
    """A COUNT with an ORDER BY in a subquery is rejected by some databases."""
    repository = LabelledInvoiceRepository(session)
    for index in range(3):
        await repository.create(label=f"l{index}")
    page = await repository.paginate(limit=2)
    assert page.total == 3
    assert len(page.items) == 2


# -- pagination without a COUNT ----------------------------------------


async def test_count_free_pagination_issues_no_count(recorded) -> None:  # type: ignore[no-untyped-def]
    """The COUNT was paid on every list response to draw one "next" button."""
    session, statements = recorded
    repository = InvoiceRepository(session)
    for index in range(5):
        await repository.create(label=f"row-{index}")
    statements.clear()

    page = await repository.paginate(limit=2, with_total=False)

    assert [s for s in statements if "count" in s.lower()] == []
    assert page.total is None
    assert page.has_more is True
    assert [i.label for i in page.items] == ["row-0", "row-1"]


async def test_count_free_pagination_still_knows_the_last_page(session) -> None:  # type: ignore[no-untyped-def]
    """limit + 1: the extra row is the answer, and it is never returned."""
    repository = InvoiceRepository(session)
    for index in range(4):
        await repository.create(label=f"row-{index}")

    last = await repository.paginate(limit=2, offset=2, with_total=False)
    assert len(last.items) == 2
    assert last.has_more is False


async def test_counting_pagination_still_counts(recorded) -> None:  # type: ignore[no-untyped-def]
    """The default must not change under anyone."""
    session, statements = recorded
    repository = InvoiceRepository(session)
    await repository.create(label="a")
    statements.clear()

    page = await repository.paginate(limit=1)
    assert [s for s in statements if "count" in s.lower()] != []
    assert page.total == 1


def test_a_page_without_a_total_must_know_whether_more_follow() -> None:
    with pytest.raises(ValueError, match="more rows follow"):
        Page(items=[], total=None, limit=10, offset=0)


# -- keyset pagination -------------------------------------------------


async def test_keyset_walks_every_row_exactly_once(recorded) -> None:  # type: ignore[no-untyped-def]
    session, statements = recorded
    repository = InvoiceRepository(session)
    for index in range(7):
        await repository.create(label=f"row-{index}")
    statements.clear()

    seen: list[str] = []
    cursor = None
    while True:
        page = await repository.paginate_keyset(limit=3, after=cursor)
        seen.extend(i.label for i in page.items)
        if not page.has_more:
            break
        cursor = page.next_cursor

    assert seen == [f"row-{index}" for index in range(7)]
    assert [s for s in statements if "count" in s.lower()] == []
    # Every page but the first is positioned by a WHERE, not by a skip count.
    # SQLite renders `LIMIT ? OFFSET ?` whatever the offset is, so the absence
    # of the word OFFSET proves nothing here; the predicate does.
    assert len(statements) == 3, statements
    assert [s for s in statements[1:] if "WHERE" in s] == statements[1:]


async def test_keyset_keeps_the_tenant_filter(session) -> None:  # type: ignore[no-untyped-def]
    """Losing the tenant where clause here is a data leak, not a paging bug."""
    mine = InvoiceRepository(session, tenant_id="t1")
    theirs = InvoiceRepository(session, tenant_id="t2")
    for index in range(4):
        await mine.create(label=f"mine-{index}")
        await theirs.create(label=f"theirs-{index}")

    seen: list[str] = []
    cursor = None
    while True:
        page = await mine.paginate_keyset(limit=2, after=cursor)
        seen.extend(i.label for i in page.items)
        if not page.has_more:
            break
        cursor = page.next_cursor

    assert seen == ["mine-0", "mine-1", "mine-2", "mine-3"]


async def test_keyset_honours_a_descending_order(session) -> None:  # type: ignore[no-untyped-def]
    repository = NewestInvoiceRepository(session)
    for label in ("a", "b", "c", "d"):
        await repository.create(label=label)

    first = await repository.paginate_keyset(limit=2)
    second = await repository.paginate_keyset(limit=2, after=first.next_cursor)
    assert [i.label for i in first.items] == ["d", "c"]
    assert [i.label for i in second.items] == ["b", "a"]


async def test_keyset_survives_duplicate_sort_keys(session) -> None:
    """Ties are why the primary key is appended: without it a row is dropped."""
    repository = LabelledInvoiceRepository(session)
    for _ in range(5):
        await repository.create(label="same")

    seen: list[int] = []
    cursor = None
    while True:
        page = await repository.paginate_keyset(limit=2, after=cursor)
        seen.extend(i.id for i in page.items)
        if not page.has_more:
            break
        cursor = page.next_cursor

    assert sorted(seen) == seen
    assert len(set(seen)) == 5


async def test_keyset_rejects_a_cursor_of_the_wrong_width(session) -> None:  # type: ignore[no-untyped-def]
    repository = InvoiceRepository(session)
    with pytest.raises(ValueError, match="cursor"):
        await repository.paginate_keyset(limit=2, after=(1, 2, 3))


# -- timestamps --------------------------------------------------------


def test_the_timestamp_ddl_asks_for_a_zone() -> None:
    """TIMESTAMP WITHOUT TIME ZONE serialises with no offset at all.

    Every JavaScript client then reads the value as local time, so a row
    written now renders hours away for anyone not sitting on UTC.
    """
    ddl = str(CreateTable(Stamped.__table__).compile(dialect=postgresql.dialect()))
    assert "WITHOUT TIME ZONE" not in ddl, ddl
    assert ddl.count("TIMESTAMP WITH TIME ZONE") == 2, ddl


async def test_timestamps_are_read_back_aware(session) -> None:  # type: ignore[no-untyped-def]
    repository = StampedRepository(session)
    row = await repository.create()

    assert row.created_at.tzinfo is not None
    assert row.updated_at.tzinfo is not None


async def test_timestamps_serialise_with_an_offset(session) -> None:  # type: ignore[no-untyped-def]
    repository = StampedRepository(session)
    row = await repository.create()

    assert row.created_at.isoformat().endswith("+00:00")


async def test_a_naive_datetime_is_refused_rather_than_stored(session) -> None:  # type: ignore[no-untyped-def]
    """There is no correct zone to assume, so guessing one is not an option."""
    repository = StampedRepository(session)
    # SQLAlchemy wraps a bind-time error in StatementError; the ValueError the
    # column raised is its __cause__.
    with pytest.raises(StatementError, match="naive datetime") as raised:
        await repository.create(created_at=datetime(2026, 1, 1, 12, 0))
    assert isinstance(raised.value.orig, ValueError)


# -- the rest ----------------------------------------------------------


async def test_get_or_raise(session) -> None:  # type: ignore[no-untyped-def]
    repository = InvoiceRepository(session)
    created = await repository.create(label="x")
    assert (await repository.get_or_raise(created.id)).label == "x"
    with pytest.raises(NotFoundError):
        await repository.get_or_raise(9999)


async def test_update_and_delete(session) -> None:  # type: ignore[no-untyped-def]
    repository = InvoiceRepository(session)
    invoice = await repository.create(label="before")
    await repository.update(invoice, label="after")
    assert invoice.label == "after"

    await repository.delete(invoice)
    assert await repository.count() == 0
