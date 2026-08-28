"""MongoDB via Motor.

Publishes ``mongo.client`` and ``mongo.db``. Use it for document-shaped data
that does not want a schema -- chat histories, event payloads, scraped
documents. It is not a replacement for the ``database`` plugin: relational data
with foreign keys belongs in PostgreSQL, and running both is fine.

Requires: ``pip install jfastframework[mongo]``
"""

from __future__ import annotations

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


class MongoSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_MONGO_", env_file=".env", extra="ignore")

    dsn: SecretStr = SecretStr("mongodb://localhost:27017")
    database: str = "app"
    max_pool_size: int = 50
    server_selection_timeout_ms: int = 5000

    include_infra: bool = True
    image: str = "mongo:7"
    port_offset: int = 4


class MongoPlugin(Plugin):
    meta = PluginMeta(
        name="mongo",
        version="0.1.0",
        description="MongoDB client and database handle via Motor.",
        after=("observability",),
        provides=("mongo.client", "mongo.db"),
        default_enabled=False,
        extra="jfastframework[mongo]",
    )
    Settings = MongoSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._client: Any = None

    def register(self, ctx: AppContext) -> None:
        from motor.motor_asyncio import AsyncIOMotorClient

        settings: MongoSettings = self.settings
        client: Any = AsyncIOMotorClient(
            settings.dsn.get_secret_value(),
            maxPoolSize=settings.max_pool_size,
            serverSelectionTimeoutMS=settings.server_selection_timeout_ms,
        )
        self._client = client
        ctx.provide("mongo.client", client)
        ctx.provide("mongo.db", client[settings.database])

    async def shutdown(self, ctx: AppContext) -> None:
        if self._client is not None:
            self._client.close()

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._client is None:
            return HealthReport.fail("mongo client not initialised")
        try:
            await self._client.admin.command("ping")
        except Exception as exc:  # noqa: BLE001
            return HealthReport.fail(f"mongo unreachable: {exc}")
        return HealthReport.ok("mongo reachable", database=self.settings.database)

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        settings: MongoSettings = self.settings
        if not settings.include_infra:
            return []
        return [
            InfraService(
                name="mongo",
                image=settings.image,
                port_offset=settings.port_offset,
                internal_port=27017,
                environment={
                    "MONGO_INITDB_DATABASE": settings.database,
                    "MONGO_INITDB_ROOT_USERNAME": "root",
                    "MONGO_INITDB_ROOT_PASSWORD": "${MONGO_PASSWORD:?set MONGO_PASSWORD}",
                },
                volumes=["mongo_data:/data/db"],
                healthcheck={
                    "test": ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"],
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 5,
                },
            )
        ]
