"""BaseRepository: ordering and tenant scoping, both of which used to fail quietly.

Runs against SQLite in memory. What is being tested is the SQL the repository
builds and the guardrails around it, not PostgreSQL behaviour.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jfastframework.db.repository import BaseRepository
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


class InvoiceRepository(BaseRepository[Invoice]):
    model = Invoice


class CurrencyRepository(BaseRepository[Currency]):
    model = Currency
    tenant_scoped = False


class LabelledInvoiceRepository(BaseRepository[Invoice]):
    model = Invoice
    order_by = ("label",)


@pytest.fixture
async def session():  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as open_session:
        yield open_session
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
