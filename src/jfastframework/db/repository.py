"""Generic async repository.

Kills the per-module CRUD boilerplate. Subclass it and you get list, get,
create, update, delete and pagination typed to your model::

    class OrderRepository(BaseRepository[Order]):
        model = Order

        async def pending(self) -> list[Order]:
            return await self.find(status="pending")

Two rules this class enforces rather than assumes, because both fail silently
when they are only conventions:

**Pagination is ordered.** ``LIMIT``/``OFFSET`` without an ``ORDER BY`` does
not give stable pages in PostgreSQL. The planner is free to return rows in a
different order on the second query, so a row can appear on page one and page
two while another is never returned at all. The primary key is the default;
override ``order_by`` when the natural order is something else.

**Tenant scoping cannot silently do nothing.** A repository handed a tenant id
for a model with no ``tenant_id`` column used to return every row of every
tenant. That is a data leak in the shape of a no-op, so it now raises. A model
that genuinely is global says so with ``tenant_scoped = False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from jfastframework.errors import NotFoundError

TModel = TypeVar("TModel")


@dataclass
class Page(Generic[TModel]):
    items: list[TModel]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class BaseRepository(Generic[TModel]):
    model: ClassVar[Any]
    # False for reference data every tenant shares -- currencies, countries,
    # plans. Saying so is the only way to tell "global on purpose" apart from
    # "somebody forgot the column".
    tenant_scoped: ClassVar[bool] = True
    # Columns to order by, as names. Defaults to the primary key.
    order_by: ClassVar[tuple[str, ...]] = ()

    def __init__(self, session: AsyncSession, *, tenant_id: str | None = None) -> None:
        self.session = session
        # When set, every query built by this repository is tenant-filtered.
        self.tenant_id = tenant_id
        if tenant_id is not None and self.tenant_scoped and not hasattr(self.model, "tenant_id"):
            raise TypeError(
                f"{type(self).__name__} was given a tenant_id but {self.model.__name__} "
                f"has no tenant_id column. Add the column, or declare "
                f"tenant_scoped = False if this model is shared across tenants."
            )

    # -- query building ------------------------------------------------

    def _order_columns(self) -> list[Any]:
        if self.order_by:
            return [getattr(self.model, name) for name in self.order_by]
        return list(inspect(self.model).primary_key)

    def _base_query(self) -> Any:
        query = select(self.model)
        if self.tenant_id is not None and self.tenant_scoped:
            query = query.where(self.model.tenant_id == self.tenant_id)
        return query

    def _filtered(self, filters: dict[str, Any]) -> Any:
        query = self._base_query()
        for field, value in filters.items():
            query = query.where(getattr(self.model, field) == value)
        return query

    # -- reads ---------------------------------------------------------

    async def get(self, pk: Any) -> TModel | None:
        query = self._base_query().where(self.model.id == pk)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def get_or_raise(self, pk: Any) -> TModel:
        instance = await self.get(pk)
        if instance is None:
            raise NotFoundError(f"{self.model.__name__} {pk!r} not found")
        return instance

    async def find(self, **filters: Any) -> list[TModel]:
        query = self._filtered(filters).order_by(*self._order_columns())
        return list((await self.session.execute(query)).scalars().all())

    async def find_one(self, **filters: Any) -> TModel | None:
        results = await self.find(**filters)
        return results[0] if results else None

    async def paginate(self, *, limit: int = 50, offset: int = 0, **filters: Any) -> Page[TModel]:
        query = self._filtered(filters)

        # Count before ordering: the ORDER BY is dead weight in a COUNT, and
        # some databases refuse it inside a subquery.
        total_query = select(func.count()).select_from(query.subquery())
        total = (await self.session.execute(total_query)).scalar_one()

        ordered = query.order_by(*self._order_columns()).limit(limit).offset(offset)
        result = await self.session.execute(ordered)
        return Page(
            items=list(result.scalars().all()),
            total=int(total),
            limit=limit,
            offset=offset,
        )

    async def count(self, **filters: Any) -> int:
        query = self._filtered(filters)
        total = await self.session.execute(select(func.count()).select_from(query.subquery()))
        return int(total.scalar_one())

    # -- writes --------------------------------------------------------

    async def create(self, **values: Any) -> TModel:
        if self.tenant_id is not None and self.tenant_scoped:
            values.setdefault("tenant_id", self.tenant_id)
        instance = self.model(**values)
        self.session.add(instance)
        # flush, not commit: the request-scoped session owns the transaction.
        await self.session.flush()
        return instance  # type: ignore[no-any-return]

    async def update(self, instance: TModel, **values: Any) -> TModel:
        for field, value in values.items():
            setattr(instance, field, value)
        await self.session.flush()
        return instance

    async def delete(self, instance: TModel) -> None:
        await self.session.delete(instance)
        await self.session.flush()
