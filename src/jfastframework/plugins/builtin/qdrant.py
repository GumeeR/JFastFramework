"""Qdrant vector database.

Publishes ``qdrant.client``. Enable it alongside ``rag`` and set
``[plugin.rag] store = "qdrant"`` to move retrieval off PostgreSQL::

    [plugins]
    enabled = ["observability", "qdrant", "rag"]

    [plugin.rag]
    store = "qdrant"

Requires: ``pip install jfastframework[qdrant]``
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


class QdrantSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_QDRANT_", env_file=".env", extra="ignore")

    url: str = "http://localhost:6333"
    api_key: SecretStr | None = None
    timeout: float = 30.0
    prefer_grpc: bool = False

    include_infra: bool = True
    image: str = "qdrant/qdrant:v1.12.4"
    http_port_offset: int = 7
    grpc_port_offset: int = 8


class QdrantPlugin(Plugin):
    meta = PluginMeta(
        name="qdrant",
        version="0.1.0",
        description="Qdrant vector database client and container.",
        after=("observability",),
        provides=("qdrant.client",),
        default_enabled=False,
        extra="jfastframework[qdrant]",
    )
    Settings = QdrantSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._client: Any = None

    def register(self, ctx: AppContext) -> None:
        from qdrant_client import AsyncQdrantClient

        settings: QdrantSettings = self.settings
        client = AsyncQdrantClient(
            url=settings.url,
            api_key=settings.api_key.get_secret_value() if settings.api_key else None,
            timeout=int(settings.timeout),
            prefer_grpc=settings.prefer_grpc,
        )
        self._client = client
        ctx.provide("qdrant.client", client)

    async def shutdown(self, ctx: AppContext) -> None:
        if self._client is not None:
            await self._client.close()

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._client is None:
            return HealthReport.fail("qdrant client not initialised")
        try:
            await self._client.get_collections()
        except Exception as exc:  # noqa: BLE001
            return HealthReport.fail(f"qdrant unreachable: {exc}")
        return HealthReport.ok("qdrant reachable", url=self.settings.url)

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        settings: QdrantSettings = self.settings
        if not settings.include_infra:
            return []
        return [
            InfraService(
                name="qdrant",
                image=settings.image,
                port_offset=settings.http_port_offset,
                internal_port=6333,
                extra_ports=[(settings.grpc_port_offset, 6334)],
                volumes=["qdrant_data:/qdrant/storage"],
                environment={"QDRANT__SERVICE__ENABLE_TLS": "false"},
                # The HTTP port. gRPC is published too, but `url` is what the
                # client reads and `prefer_grpc` is a separate setting.
                client_env={"JFAST_QDRANT_URL": "http://qdrant:6333"},
            )
        ]
