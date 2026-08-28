"""Retrieval-Augmented Generation with a pluggable vector store.

Two things are swappable here, and neither requires touching the router:

* the **store** -- ``pgvector`` (default) or ``qdrant``, or your own class;
* the **embedder** -- ``ollama`` (default), or your own class.

::

    [plugins]
    enabled = ["observability", "qdrant", "rag"]

    [plugin.rag]
    store = "qdrant"
    embedder = "myapp.embeddings:OpenAIEmbedder"

Backend dependencies are checked at build time with an actionable message:
choosing ``pgvector`` without the ``database`` plugin, or ``qdrant`` without
the ``qdrant`` plugin, fails at startup rather than at the first search.

Status: both stores and the Ollama embedder are implemented. Chunking is
fixed-size with overlap -- adequate for prose, not for code or tables. No
reranking, no hybrid search. See PLAN.md phase 3.

Requires: ``pip install jfastframework[rag]`` plus the extra for the store.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from fastapi import APIRouter
from pydantic import BaseModel, Field
from pydantic_settings import SettingsConfigDict

from jfastframework.errors import PluginError, ServiceUnavailableError
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings
from jfastframework.vectors.base import Chunk, VectorStore

if TYPE_CHECKING:
    from jfastframework.context import AppContext


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into vectors."""

    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    """Local embeddings through an Ollama server."""

    def __init__(self, base_url: str, model: str, dimensions: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        async with httpx.AsyncClient(base_url=self.base_url, timeout=60.0) as client:
            response = await client.post("/api/embed", json={"model": self.model, "input": texts})
            if response.status_code >= 400:
                raise ServiceUnavailableError(
                    f"Ollama embedding failed ({response.status_code}): {response.text[:200]}"
                )
            return list(response.json()["embeddings"])


def chunk_text(text: str, *, size: int, overlap: int) -> list[str]:
    """Fixed-size character chunking with overlap.

    Deliberately dumb. Structure-aware splitters are phase 3; swapping this out
    must not change the store's interface.
    """
    if size <= 0:
        raise ValueError("chunk size must be positive")
    if overlap >= size:
        raise ValueError("overlap must be smaller than chunk size")

    stride = size - overlap
    chunks: list[str] = []
    start = 0
    while start < len(text):
        piece = text[start : start + size].strip()
        if piece:
            chunks.append(piece)
        # Stop once the window reaches the end. Striding past it would emit a
        # tail already fully contained in the previous chunk -- a duplicate
        # sliver that costs an embedding call and pollutes the results.
        if start + size >= len(text):
            break
        start += stride
    return chunks


def _load_class(path: str, what: str) -> Any:
    module_path, _, attr = path.partition(":")
    if not module_path or not attr:
        raise PluginError(
            f"Invalid {what} {path!r}. Use a built-in name or 'package.module:ClassName'."
        )
    try:
        return getattr(importlib.import_module(module_path), attr)
    except (ImportError, AttributeError) as exc:
        raise PluginError(f"Cannot load {what} {path!r}: {exc}") from exc


class RagSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_RAG_", env_file=".env", extra="ignore")

    # "pgvector" | "qdrant" | "package.module:ClassName"
    store: str = "pgvector"
    # "ollama" | "package.module:ClassName"
    embedder: str = "ollama"

    # Table name for pgvector, collection name for Qdrant.
    collection: str = "rag_chunks"
    dimensions: int = 768
    chunk_size: int = 1000
    chunk_overlap: int = 150
    top_k: int = 5

    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "nomic-embed-text"

    mount_router: bool = True
    prefix: str = "/rag"
    # Creates the table/collection on startup. Convenient in development; turn
    # it off in production and manage schema through migrations.
    auto_migrate: bool = True


class IngestRequest(BaseModel):
    document_id: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str | None = None


class SearchRequest(BaseModel):
    query: str
    limit: int | None = None
    tenant_id: str | None = None


class RagPlugin(Plugin):
    meta = PluginMeta(
        name="rag",
        version="0.2.0",
        description="Retrieval over a pluggable vector store (pgvector or Qdrant).",
        # Not `requires`: which backend this needs depends on configuration, so
        # the check lives in register() where the config is known. `after`
        # still guarantees the backend plugin starts first when enabled.
        after=("observability", "database", "qdrant"),
        provides=("rag.store", "rag.embedder"),
        default_enabled=False,
        extra="jfastframework[rag]",
    )
    Settings = RagSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._store: VectorStore | None = None
        self._embedder: Embedder | None = None
        self._setup_error: str | None = None

    # -- construction --------------------------------------------------

    def _build_embedder(self) -> Embedder:
        settings: RagSettings = self.settings
        if settings.embedder == "ollama":
            return OllamaEmbedder(
                base_url=settings.ollama_url,
                model=settings.ollama_model,
                dimensions=settings.dimensions,
            )
        embedder: Embedder = _load_class(settings.embedder, "embedder")()
        return embedder

    def _build_store(self, ctx: AppContext) -> VectorStore:
        settings: RagSettings = self.settings

        if settings.store == "pgvector":
            if not ctx.has("db.engine"):
                raise PluginError(
                    "rag store 'pgvector' needs the 'database' plugin. "
                    'Add "database" to [plugins].enabled, or set '
                    '[plugin.rag] store = "qdrant".'
                )
            from jfastframework.vectors.pgvector import PgVectorStore

            return PgVectorStore(
                ctx.require("db.engine"),
                table=settings.collection,
                dimensions=settings.dimensions,
            )

        if settings.store == "qdrant":
            if not ctx.has("qdrant.client"):
                raise PluginError(
                    "rag store 'qdrant' needs the 'qdrant' plugin. "
                    'Add "qdrant" to [plugins].enabled.'
                )
            from jfastframework.vectors.qdrant import QdrantStore

            return QdrantStore(
                ctx.require("qdrant.client"),
                collection=settings.collection,
                dimensions=settings.dimensions,
            )

        store: VectorStore = _load_class(settings.store, "vector store")(ctx)
        return store

    # -- lifecycle -----------------------------------------------------

    def register(self, ctx: AppContext) -> None:
        settings: RagSettings = self.settings
        self._embedder = self._build_embedder()
        self._store = self._build_store(ctx)

        ctx.provide("rag.store", self._store)
        ctx.provide("rag.embedder", self._embedder)

        if settings.mount_router:
            ctx.app.include_router(self._build_router(), prefix=settings.prefix, tags=["rag"])

    async def startup(self, ctx: AppContext) -> None:
        # Same reasoning as the queue plugin: a store that is not up yet
        # should make the service unready, not make it crash-loop.
        if self.settings.auto_migrate and self._store is not None:
            try:
                await self._store.ensure_schema()
            except Exception as exc:  # noqa: BLE001 - reported through /ready
                self._setup_error = str(exc)
                ctx.logger.error(
                    "rag schema setup failed; the service is serving but not ready",
                    extra={"store": self.settings.store, "error": str(exc)},
                )
            else:
                self._setup_error = None

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._store is None:
            return HealthReport.fail("rag store not initialised")
        if self._setup_error is not None:
            return HealthReport.fail(f"rag schema setup failed: {self._setup_error}")
        healthy, detail = await self._store.health()
        meta = {"store": self.settings.store, "embedder": self.settings.embedder}
        return HealthReport.ok(detail, **meta) if healthy else HealthReport.fail(detail, **meta)

    # -- http ----------------------------------------------------------

    def _build_router(self) -> APIRouter:
        router = APIRouter()
        settings: RagSettings = self.settings

        @router.post("/documents", summary="Ingest or replace a document")
        async def ingest(payload: IngestRequest) -> dict[str, Any]:
            store, embedder = self._require_ready()
            pieces = chunk_text(
                payload.content, size=settings.chunk_size, overlap=settings.chunk_overlap
            )
            if not pieces:
                return {"document_id": payload.document_id, "chunks": 0}

            embeddings = await embedder.embed(pieces)
            chunks = [
                Chunk(
                    document_id=payload.document_id,
                    chunk_index=index,
                    content=piece,
                    metadata=payload.metadata,
                    tenant_id=payload.tenant_id,
                )
                for index, piece in enumerate(pieces)
            ]
            count = await store.upsert(chunks, embeddings)
            return {"document_id": payload.document_id, "chunks": count}

        @router.post("/search", summary="Semantic search")
        async def search(payload: SearchRequest) -> dict[str, Any]:
            store, embedder = self._require_ready()
            vector = (await embedder.embed([payload.query]))[0]
            hits = await store.search(
                vector,
                limit=payload.limit or settings.top_k,
                tenant_id=payload.tenant_id,
            )
            return {"query": payload.query, "results": [hit.as_dict() for hit in hits]}

        @router.delete("/documents/{document_id}", summary="Delete a document")
        async def delete(document_id: str) -> dict[str, str]:
            store, _ = self._require_ready()
            await store.delete_document(document_id)
            return {"status": "deleted", "document_id": document_id}

        return router

    def _require_ready(self) -> tuple[VectorStore, Embedder]:
        if self._store is None or self._embedder is None:
            raise ServiceUnavailableError("rag plugin is not initialised")
        return self._store, self._embedder
