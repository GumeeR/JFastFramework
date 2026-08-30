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

Three ways to page, in rising order of what they cost the database:

``paginate(..., with_total=False)`` is the one most list endpoints want. It
fetches ``limit + 1`` rows and returns ``limit`` of them, so it answers "is
there another page" without the COUNT -- which, on a table worth paginating,
is the expensive half of the response and is usually thrown away.

``paginate_keyset(after=...)`` is the one deep pages want. ``OFFSET n`` makes
the database walk and discard n rows before it returns anything, so page 200
costs 200 pages of work; a keyset page is a range scan from a known point and
costs the same wherever it lands. The trade is random access: there is a next
page, not a page 40.

``paginate()`` unchanged, exact total, one COUNT. Keep it where a client
genuinely renders "1-50 of 4,812" and can afford it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

from sqlalchemy import and_, func, inspect, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from jfastframework.errors import NotFoundError

TModel = TypeVar("TModel")

# The ordering values of the last row on a page, in order-column order.
# Opaque to callers: hand it back to `paginate_keyset(after=...)` untouched.
# Encoding it for a URL is the application's call, because whether it needs
# signing depends on whether the ordering columns are secrets.
Cursor = tuple[Any, ...]


@dataclass
class Page(Generic[TModel]):
    """One page of rows, and enough to ask for the next one.

    ``total`` is ``None`` on every page fetched without a COUNT. That is not a
    value waiting to be filled in later -- the query deliberately never asked,
    so nothing downstream can render "page 3 of 40" out of it.
    """

    items: list[TModel]
    total: int | None
    limit: int
    offset: int
    # Set by the modes that skip the COUNT: they read one row past the page, so
    # "is there a next page" is answered without counting the rows behind it.
    # None means the answer comes from `total` instead.
    more: bool | None = None
    # Feed back to `paginate_keyset(after=...)`. None on the last page, and on
    # every page that was not keyset-paginated.
    next_cursor: Cursor | None = None

    def __post_init__(self) -> None:
        if self.total is None and self.more is None:
            raise ValueError(
                "a Page with no total must say whether more rows follow: pass more=True/False"
            )

    @property
    def has_more(self) -> bool:
        if self.more is not None:
            return self.more
        total = self.total
        return total is not None and self.offset + len(self.items) < total


class BaseRepository(Generic[TModel]):
    model: ClassVar[Any]
    # False for reference data every tenant shares -- currencies, countries,
    # plans. Saying so is the only way to tell "global on purpose" apart from
    # "somebody forgot the column".
    tenant_scoped: ClassVar[bool] = True
    # Columns to order by, as names. A leading "-" sorts that column
    # descending. Defaults to the primary key, ascending.
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

    def _order_spec(self) -> list[tuple[Any, bool]]:
        """``(column, descending)`` pairs.

        Direction is carried alongside the column rather than baked into it:
        keyset paging has to know which way each column sorts to build the
        comparison, and an ``UnaryExpression`` from ``.desc()`` will not say.
        """
        if self.order_by:
            spec: list[tuple[Any, bool]] = []
            for name in self.order_by:
                descending = name.startswith("-")
                spec.append((getattr(self.model, name[1:] if descending else name), descending))
            return spec
        return [(column, False) for column in inspect(self.model).primary_key]

    def _keyset_spec(self) -> list[tuple[Any, bool]]:
        """The order spec with the primary key appended as a tiebreaker.

        Keyset paging compares each row against the last row of the page
        before it, so the ordering has to be a *total* order. Two rows that
        compare equal straddle the boundary in whatever sequence the planner
        picked, and one of them is dropped from both pages -- the same silent
        row loss ``ORDER BY`` was added to prevent, one level down.

        The tiebreaker inherits the direction of the column it follows, so a
        newest-first feed stays uniformly descending and keeps the row-value
        comparison below.
        """
        spec = self._order_spec()
        present = {column.key for column, _ in spec}
        trailing_descending = spec[-1][1] if spec else False
        for column in inspect(self.model).primary_key:
            if column.key not in present:
                spec.append((column, trailing_descending))
        return spec

    @staticmethod
    def _directed(spec: list[tuple[Any, bool]]) -> list[Any]:
        return [column.desc() if descending else column.asc() for column, descending in spec]

    def _order_columns(self) -> list[Any]:
        return self._directed(self._order_spec())

    @staticmethod
    def _after_cursor(spec: list[tuple[Any, bool]], cursor: Cursor) -> Any:
        """Rows strictly past ``cursor`` in the order ``spec`` describes.

        Row-value comparison when every column sorts the same way, because
        PostgreSQL turns ``(a, b) > (:a, :b)`` into a single index range scan
        on a composite index -- the whole point of paging this way. Mixed
        directions have no row-value spelling, so they expand to the OR chain,
        which is correct on every backend and merely slower.

        Either form assumes the ordering columns are NOT NULL. A NULL compares
        UNKNOWN in both, and the page it sits on simply ends early.
        """
        columns = [column for column, _ in spec]
        directions = {descending for _, descending in spec}
        if len(directions) == 1:
            row = tuple_(*columns)
            values = tuple_(*cursor)
            return row < values if directions.pop() else row > values

        clauses = []
        for index, (column, descending) in enumerate(spec):
            ties = [earlier == cursor[i] for i, (earlier, _) in enumerate(spec[:index])]
            step = column < cursor[index] if descending else column > cursor[index]
            clauses.append(and_(*ties, step))
        return or_(*clauses)

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

    async def paginate(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        with_total: bool = True,
        **filters: Any,
    ) -> Page[TModel]:
        """One page by offset. ``with_total=False`` drops the COUNT.

        The default still counts, because changing what an existing caller
        gets back is worse than making it opt in.
        """
        query = self._filtered(filters)
        ordered = query.order_by(*self._order_columns())

        if not with_total:
            # limit + 1: the extra row is the whole answer and is never
            # returned. It costs one row, against a COUNT that visits every
            # row behind the page to produce a number most clients only use
            # to decide whether to draw a "next" button.
            rows = list(
                (await self.session.execute(ordered.limit(limit + 1).offset(offset)))
                .scalars()
                .all()
            )
            return Page(
                items=rows[:limit],
                total=None,
                limit=limit,
                offset=offset,
                more=len(rows) > limit,
            )

        # Count before ordering: the ORDER BY is dead weight in a COUNT, and
        # some databases refuse it inside a subquery.
        total_query = select(func.count()).select_from(query.subquery())
        total = (await self.session.execute(total_query)).scalar_one()

        result = await self.session.execute(ordered.limit(limit).offset(offset))
        return Page(
            items=list(result.scalars().all()),
            total=int(total),
            limit=limit,
            offset=offset,
        )

    async def paginate_keyset(
        self, *, limit: int = 50, after: Cursor | None = None, **filters: Any
    ) -> Page[TModel]:
        """One page positioned by the last row of the previous one.

        Built on ``_filtered`` like every other read, so the tenant ``where``
        clause is applied here too. It has to be: a keyset page that quietly
        dropped it would serve another tenant's rows on page two while page
        one looked correct.

        ``Page.offset`` is 0 on every keyset page. There is no offset to
        report -- that is the cost this method exists to avoid -- so read
        ``has_more`` and ``next_cursor``, never ``offset``.
        """
        spec = self._keyset_spec()
        query = self._filtered(filters)
        if after is not None:
            if len(after) != len(spec):
                raise ValueError(
                    f"cursor has {len(after)} values but the ordering needs "
                    f"{len(spec)}; it belongs to a different query"
                )
            query = query.where(self._after_cursor(spec, after))

        rows = list(
            (await self.session.execute(query.order_by(*self._directed(spec)).limit(limit + 1)))
            .scalars()
            .all()
        )
        items = rows[:limit]
        more = len(rows) > limit
        return Page(
            items=items,
            total=None,
            limit=limit,
            offset=0,
            more=more,
            next_cursor=self.cursor_for(items[-1]) if more and items else None,
        )

    def cursor_for(self, instance: TModel) -> Cursor:
        """The cursor that resumes paging just after ``instance``."""
        return tuple(getattr(instance, column.key) for column, _ in self._keyset_spec())

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
