"""Async PostgreSQL via SQLAlchemy 2.0.

Publishes ``db.engine`` and ``db.sessionmaker``. Route handlers get a session
through the ``session_dependency`` FastAPI dependency, which commits on success
and rolls back on any exception.

The DSN is read from ``JFAST_DB_DSN`` and is a ``SecretStr`` -- it is never
echoed by ``jfast describe`` or by ``/info``. Do not put it in ``jfast.toml``.

Requires: ``pip install jfastframework[db]``
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from jfastframework.plugins.base import (
    HealthReport,
    InfraService,
    Plugin,
    PluginMeta,
    PluginSettings,
)

if TYPE_CHECKING:
    from jfastframework.context import AppContext


class DatabaseSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_DB_", env_file=".env", extra="ignore")

    dsn: SecretStr = SecretStr("postgresql+asyncpg://postgres:postgres@localhost:5432/postgres")
    echo: bool = False
    pool_size: int = 10
    max_overflow: int = 20
    pool_pre_ping: bool = True
    pool_recycle: int = 1800

    # Deploy generation
    include_infra: bool = True
    image: str = "pgvector/pgvector:pg16"
    port_offset: int = 1
    database: str = "app"
    user: str = "app"


class DatabasePlugin(Plugin):
    meta = PluginMeta(
        name="database",
        version="0.1.0",
        description="Async SQLAlchemy engine, session factory and request-scoped sessions.",
        after=("observability",),
        provides=("db.engine", "db.sessionmaker"),
        default_enabled=False,
        extra="jfastframework[db]",
    )
    Settings = DatabaseSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._engine: Any = None

    def register(self, ctx: AppContext) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        settings: DatabaseSettings = self.settings
        engine = create_async_engine(
            settings.dsn.get_secret_value(),
            echo=settings.echo,
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            pool_pre_ping=settings.pool_pre_ping,
            pool_recycle=settings.pool_recycle,
        )
        # expire_on_commit=False keeps ORM objects usable after the request
        # scope commits, which is what response serialisation needs.
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        self._engine = engine
        ctx.provide("db.engine", engine)
        ctx.provide("db.sessionmaker", sessionmaker)

    async def shutdown(self, ctx: AppContext) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    async def health(self, ctx: AppContext) -> HealthReport:
        from sqlalchemy import text

        if self._engine is None:
            return HealthReport.fail("engine not initialised")
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            return HealthReport.fail(f"database unreachable: {exc}")
        return HealthReport.ok("database reachable")

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        settings: DatabaseSettings = self.settings
        if not settings.include_infra:
            return []
        return [
            InfraService(
                name="postgres",
                image=settings.image,
                port_offset=settings.port_offset,
                internal_port=5432,
                environment={
                    "POSTGRES_DB": settings.database,
                    "POSTGRES_USER": settings.user,
                    "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}",
                },
                volumes=["postgres_data:/var/lib/postgresql/data"],
                healthcheck={
                    "test": ["CMD-SHELL", f"pg_isready -U {settings.user}"],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 10,
                },
            )
        ]


async def session_dependency(request: Any) -> AsyncIterator[Any]:
    """FastAPI dependency yielding a request-scoped session.

    Usage::

        from fastapi import Depends
        from jfastframework.plugins.builtin.database import session_dependency

        @router.get("/items")
        async def list_items(session = Depends(session_dependency)):
            ...
    """
    ctx = request.app.state.jfast
    sessionmaker = ctx.require("db.sessionmaker")
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
