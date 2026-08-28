"""Generic async repository.

Kills the per-module CRUD boilerplate. Subclass it and you get list, get,
create, update, delete and pagination typed to your model::

    class OrderRepository(BaseRepository[Order]):
        model = Order

        async def pending(self) -> list[Order]:
            return await self.find(status="pending")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

from sqlalchemy import func, select
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

    def __init__(self, session: AsyncSession, *, tenant_id: str | None = None) -> None:
        self.session = session
        # When set, every query built by this repository is tenant-filtered.
        self.tenant_id = tenant_id

    def _base_query(self) -> Any:
        query = select(self.model)
        if self.tenant_id is not None and hasattr(self.model, "tenant_id"):
            query = query.where(self.model.tenant_id == self.tenant_id)
        return query

    async def get(self, pk: Any) -> TModel | None:
        query = self._base_query().where(self.model.id == pk)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def get_or_raise(self, pk: Any) -> TModel:
        instance = await self.get(pk)
        if instance is None:
            raise NotFoundError(f"{self.model.__name__} {pk!r} not found")
        return instance

    async def find(self, **filters: Any) -> list[TModel]:
        query = self._base_query()
        for field, value in filters.items():
            query = query.where(getattr(self.model, field) == value)
        return list((await self.session.execute(query)).scalars().all())

    async def find_one(self, **filters: Any) -> TModel | None:
        results = await self.find(**filters)
        return results[0] if results else None

    async def paginate(self, *, limit: int = 50, offset: int = 0, **filters: Any) -> Page[TModel]:
        query = self._base_query()
        for field, value in filters.items():
            query = query.where(getattr(self.model, field) == value)

        total_query = select(func.count()).select_from(query.subquery())
        total = (await self.session.execute(total_query)).scalar_one()

        result = await self.session.execute(query.limit(limit).offset(offset))
        return Page(
            items=list(result.scalars().all()),
            total=int(total),
            limit=limit,
            offset=offset,
        )

    async def create(self, **values: Any) -> TModel:
        if self.tenant_id is not None and hasattr(self.model, "tenant_id"):
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

    async def count(self, **filters: Any) -> int:
        query = self._base_query()
        for field, value in filters.items():
            query = query.where(getattr(self.model, field) == value)
        total = await self.session.execute(select(func.count()).select_from(query.subquery()))
        return int(total.scalar_one())
