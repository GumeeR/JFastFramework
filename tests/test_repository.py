"""BaseRepository: ordering and tenant scoping, both of which used to fail quietly.

Runs against SQLite in memory. What is being tested is the SQL the repository
builds and the guardrails around it, not PostgreSQL behaviour -- with two
exceptions. The timestamp DDL is compiled against the PostgreSQL dialect,
because ``TIMESTAMP WITHOUT TIME ZONE`` is a PostgreSQL type name and SQLite
would render neither spelling. And the NULL-ordering tests run twice, once per
backend, because the two disagree about where NULLs sort by default and a
keyset walk verified on only one of them is not verified: the same code that
lost 180 of 200 rows on SQLite lost 40 on PostgreSQL, and a fix that made one
of those numbers zero would have looked complete.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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


class Message(Base):
    """A feed row sorted by a nullable column, which is the ordinary case.

    ``edited_at`` stands in for ``last_message_at``/``edited_at``: it is NULL
    until something edits the row, so most of the table has no sort key at all.
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    edited_at: Mapped[int | None] = mapped_column(default=None)
    room: Mapped[str] = mapped_column(default="a")


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


class EditedOldestRepository(BaseRepository[Message]):
    model = Message
    tenant_scoped = False
    order_by = ("edited_at",)


class EditedNewestRepository(BaseRepository[Message]):
    """Descending, where PostgreSQL's default puts the NULL block first."""

    model = Message
    tenant_scoped = False
    order_by = ("-edited_at",)


class RoomFeedRepository(BaseRepository[Message]):
    """Mixed directions, so the OR chain runs with a NULL inside it."""

    model = Message
    tenant_scoped = False
    order_by = ("room", "-edited_at")


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


PG_DSN = os.environ.get(
    "JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499"
).rstrip("/")


@pytest.fixture(params=["sqlite", "postgresql"])
async def either_backend(request):  # type: ignore[no-untyped-def]
    """The same session fixture, once per backend that JFastFramework targets.

    PostgreSQL sorts NULLs last ascending and first descending; SQLite sorts
    them first either way. Anything asserting where a NULL lands, or that a
    keyset walk gets past one, has to answer on both. Skipped rather than
    failed when no server answers, but the skip is the whole coverage of one
    backend -- bring one up with::

        docker run -d --name pg -p 5499:5432 \\
          -e POSTGRES_USER=jfast -e POSTGRES_PASSWORD=jfast \\
          -e POSTGRES_DB=jfast postgres:16
    """
    dsn = "sqlite+aiosqlite:///:memory:" if request.param == "sqlite" else f"{PG_DSN}/jfast"
    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception:  # noqa: BLE001 - any failure to connect means "no server here"
        await engine.dispose()
        pytest.skip(f"no PostgreSQL at {dsn}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as open_session:
        yield open_session
    await engine.dispose()


async def _seed_messages(session: AsyncSession, count: int) -> None:
    """``count`` rows, one in three never edited, so a third have no sort key."""
    repository = EditedOldestRepository(session)
    for index in range(count):
        await repository.create(
            edited_at=None if index % 3 == 0 else index,
            room="a" if index % 2 == 0 else "b",
        )
    await session.flush()


async def _walk(repository: BaseRepository[Message], *, limit: int) -> list[Message]:
    """Every row a full keyset walk reaches, and the walk's own claims checked.

    A walk that ends is not the same as a walk that ended because it ran out
    of rows, so the terminal page has to say so twice: ``has_more`` False and
    ``next_cursor`` None.
    """
    seen: list[Message] = []
    cursor = None
    for _ in range(100):
        page = await repository.paginate_keyset(limit=limit, after=cursor)
        seen.extend(page.items)
        if not page.has_more:
            assert page.next_cursor is None, "a page that says it is last handed out a cursor"
            return seen
        assert page.next_cursor is not None, "has_more with no cursor is an unreachable page"
        assert len(page.items) == limit, "a full page came back short"
        cursor = page.next_cursor
    raise AssertionError("the walk did not terminate")


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


# -- keyset pagination over a nullable ordering column -----------------
#
# The regression these cover is not a page that ends early. Ordering by a
# nullable column put a NULL in the cursor; every comparison against it is
# UNKNOWN, so the next page came back empty, `has_more` was False, and the
# walk stopped and reported the table finished. 20 of 200 rows reachable on
# SQLite, 160 of 200 on PostgreSQL, and no error either time.


async def test_keyset_reaches_every_row_when_the_sort_key_is_nullable(
    either_backend,
) -> None:  # type: ignore[no-untyped-def]
    repository = EditedOldestRepository(either_backend)
    await _seed_messages(either_backend, 21)

    seen = await _walk(repository, limit=4)
    ids = [row.id for row in seen]

    assert len(ids) == 21, f"the walk stopped at {len(ids)} of 21 rows"
    assert len(set(ids)) == 21, "a row was served on two pages"
    assert [row.edited_at for row in seen[-7:]] == [None] * 7, "the NULL block is not last"
    edited = [row.edited_at for row in seen[:14]]
    assert edited == sorted(edited), "the non-NULL rows came back out of order"


async def test_keyset_reaches_every_row_descending_over_nulls(
    either_backend,
) -> None:  # type: ignore[no-untyped-def]
    """Descending is the direction PostgreSQL defaults to NULLS FIRST."""
    repository = EditedNewestRepository(either_backend)
    await _seed_messages(either_backend, 21)

    seen = await _walk(repository, limit=4)

    assert len({row.id for row in seen}) == 21
    assert [row.edited_at for row in seen[-7:]] == [None] * 7, "the NULL block is not last"
    edited = [row.edited_at for row in seen[:14]]
    assert edited == sorted(edited, reverse=True)


async def test_keyset_reaches_every_row_with_mixed_directions_over_nulls(
    either_backend,
) -> None:  # type: ignore[no-untyped-def]
    """Mixed directions take the OR chain, which is the branch that grew ties."""
    repository = RoomFeedRepository(either_backend)
    await _seed_messages(either_backend, 21)

    seen = await _walk(repository, limit=4)

    assert len({row.id for row in seen}) == 21
    assert [row.room for row in seen] == sorted(row.room for row in seen)
    for room in ("a", "b"):
        block = [row.edited_at for row in seen if row.room == room]
        edited = [value for value in block if value is not None]
        assert block[: len(edited)] == edited, f"room {room} interleaved its NULLs"
        assert edited == sorted(edited, reverse=True)


async def test_keyset_walks_the_order_one_unpaged_query_would_return(
    either_backend,
) -> None:  # type: ignore[no-untyped-def]
    """Paging is meant to be invisible: the pages concatenated are the query.

    Compared against one unpaged SELECT rather than against offset paging,
    because ``paginate`` orders by ``order_by`` alone -- no primary-key
    tiebreaker -- so the NULL block comes back in whatever sequence the
    planner picked and only its membership is promised. Membership is the half
    that broke, so it is asserted; sequence is asserted against the total order
    keyset actually claims.
    """
    repository = EditedOldestRepository(either_backend)
    await _seed_messages(either_backend, 21)

    expected = list(
        (
            await either_backend.execute(
                select(Message).order_by(Message.edited_at.asc().nulls_last(), Message.id.asc())
            )
        )
        .scalars()
        .all()
    )

    by_offset: set[int] = set()
    offset = 0
    while True:
        page = await repository.paginate(limit=4, offset=offset, with_total=False)
        by_offset.update(row.id for row in page.items)
        if not page.has_more:
            break
        offset += 4

    walked = [row.id for row in await _walk(repository, limit=4)]
    assert walked == [row.id for row in expected]
    assert set(walked) == by_offset, "keyset and offset disagree about which rows exist"


async def test_a_cursor_holding_a_null_still_advances(
    either_backend,
) -> None:  # type: ignore[no-untyped-def]
    """The exact shape of the failure, isolated to the one page it happened on."""
    repository = EditedOldestRepository(either_backend)
    await _seed_messages(either_backend, 21)

    first = await repository.paginate_keyset(limit=15)
    assert first.next_cursor is not None
    assert first.next_cursor[0] is None, "this test is pointless unless the cursor holds a NULL"

    second = await repository.paginate_keyset(limit=15, after=first.next_cursor)
    assert second.items != [], "the page after a NULL cursor came back empty"
    assert second.has_more is False
    assert len(first.items) + len(second.items) == 21


async def test_a_cursor_of_nothing_but_nulls_is_the_end(
    either_backend,
) -> None:  # type: ignore[no-untyped-def]
    """Every column in its terminal group: there is no row after that, and no crash.

    The OR chain builds one clause per column that can be stepped past, so an
    all-NULL cursor builds none -- and an empty ``or_()`` is a TypeError, not
    an empty result.
    """
    repository = EditedOldestRepository(either_backend)
    await _seed_messages(either_backend, 6)

    page = await repository.paginate_keyset(limit=4, after=(None, None))
    assert page.items == []
    assert page.has_more is False


def test_only_nullable_ordering_columns_ask_for_nulls_last() -> None:
    """A NOT NULL column keeps the plain ORDER BY, so its index still matches.

    ``ORDER BY c DESC NULLS LAST`` cannot be answered by a plain descending
    btree index -- PostgreSQL defaults that index to NULLS FIRST -- so paying
    the clause on a column that has no NULLs buys a sort and nothing else.
    """
    nullable = EditedNewestRepository(session=None)._order_columns()  # type: ignore[arg-type]
    not_null = NewestInvoiceRepository(session=None)._order_columns()  # type: ignore[arg-type]

    assert str(nullable[0]) == "messages.edited_at DESC NULLS LAST"
    assert str(not_null[0]) == "invoices.label DESC"


def test_the_null_free_ordering_still_compiles_to_a_row_value_comparison() -> None:
    """The composite-index range scan is why keyset paging is worth having.

    It only survives where no ordering column is nullable; everything else
    takes the OR chain, which is correct everywhere and merely slower.
    """
    repository = InvoiceRepository(session=None)  # type: ignore[arg-type]
    spec = repository._keyset_spec()
    fast = str(
        select(Invoice)
        .where(repository._after_cursor(spec, (1,)))
        .compile(dialect=postgresql.dialect())
    )
    assert "(invoices.id) > (" in fast, fast

    nullable = EditedOldestRepository(session=None)  # type: ignore[arg-type]
    chain = str(
        select(Message)
        .where(nullable._after_cursor(nullable._keyset_spec(), (5, 9)))
        .compile(dialect=postgresql.dialect())
    )
    assert "IS NULL" in chain, chain
    assert " > (" not in chain, chain


async def test_offset_pagination_over_not_null_columns_is_untouched(recorded) -> None:  # type: ignore[no-untyped-def]
    """No NULLS LAST leaks into the queries that never needed it."""
    session, statements = recorded
    repository = LabelledInvoiceRepository(session)
    for index in range(3):
        await repository.create(label=f"l{index}")
    statements.clear()

    page = await repository.paginate(limit=2, offset=0)

    assert [i.label for i in page.items] == ["l0", "l1"]
    assert page.total == 3
    assert [s for s in statements if "NULLS" in s.upper()] == [], statements


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
