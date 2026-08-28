"""Qdrant-backed vector store.

Pick this over pgvector when you need payload filtering richer than a tenant
id, quantisation, or more scale than one PostgreSQL instance should carry.
The cost is a second datastore to run, back up and monitor -- do not take it
until pgvector actually stops being enough.

Point ids are derived deterministically from ``(document_id, chunk_index)`` so
re-ingesting the same document overwrites rather than duplicates.

Requires: ``pip install jfastframework[qdrant]``
"""

from __future__ import annotations

import uuid
from typing import Any

from jfastframework.vectors.base import Chunk, SearchHit

# Stable namespace for deriving point ids. Changing it orphans every existing
# point, so treat it as frozen.
_POINT_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def point_id(document_id: str, chunk_index: int) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{document_id}:{chunk_index}"))


class QdrantStore:
    def __init__(
        self,
        client: Any,
        *,
        collection: str,
        dimensions: int,
        distance: str = "Cosine",
    ) -> None:
        self._client = client
        self._collection = collection
        self._dimensions = dimensions
        self._distance = distance

    async def ensure_schema(self) -> None:
        from qdrant_client import models

        existing = await self._client.collection_exists(self._collection)
        if not existing:
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(
                    size=self._dimensions,
                    distance=models.Distance[self._distance.upper()],
                ),
            )
        # Payload index on tenant_id: without it, tenant-filtered search
        # degrades to a full scan once the collection grows.
        await self._client.create_payload_index(
            collection_name=self._collection,
            field_name="tenant_id",
            field_schema=models.PayloadSchemaType.KEYWORD,
            wait=True,
        )

    async def upsert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int:
        from qdrant_client import models

        if not chunks:
            return 0

        # Replace, not append: drop whatever the document had before.
        for document_id in {chunk.document_id for chunk in chunks}:
            await self.delete_document(document_id)

        points = [
            models.PointStruct(
                id=point_id(chunk.document_id, chunk.chunk_index),
                vector=embedding,
                payload={
                    "document_id": chunk.document_id,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "tenant_id": chunk.tenant_id,
                    "metadata": chunk.metadata,
                },
            )
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ]
        await self._client.upsert(collection_name=self._collection, points=points, wait=True)
        return len(points)

    async def search(
        self,
        embedding: list[float],
        *,
        limit: int = 5,
        tenant_id: str | None = None,
    ) -> list[SearchHit]:
        from qdrant_client import models

        query_filter = None
        if tenant_id is not None:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id))
                ]
            )

        response = await self._client.query_points(
            collection_name=self._collection,
            query=embedding,
            limit=limit,
            query_filter=query_filter,
            with_payload=True,
        )
        hits = []
        for point in response.points:
            payload = point.payload or {}
            hits.append(
                SearchHit(
                    document_id=str(payload.get("document_id", "")),
                    chunk_index=int(payload.get("chunk_index", 0)),
                    content=str(payload.get("content", "")),
                    # Cosine distance in Qdrant is already a similarity score.
                    score=float(point.score),
                    metadata=dict(payload.get("metadata") or {}),
                )
            )
        return hits

    async def delete_document(self, document_id: str) -> None:
        from qdrant_client import models

        await self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id", match=models.MatchValue(value=document_id)
                        )
                    ]
                )
            ),
            wait=True,
        )

    async def health(self) -> tuple[bool, str]:
        try:
            exists = await self._client.collection_exists(self._collection)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return False, f"qdrant unreachable: {exc}"
        if not exists:
            return False, f"qdrant collection {self._collection!r} does not exist"
        return True, f"qdrant collection {self._collection} reachable"
