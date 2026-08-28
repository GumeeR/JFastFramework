"""pgvector-backed vector store.

Default choice: vectors live in the PostgreSQL the service already runs, so
there is no second datastore to operate, back up or monitor. Good up to a few
million chunks. Past that, or when you need payload filtering beyond a tenant
id, move to Qdrant -- the ``rag`` plugin needs one config line to switch.

Requires: ``pip install jfastframework[db]``

The table name is interpolated because no database binds an identifier as
a parameter. safe_identifier() validates it at construction, and every
value is bound -- hence the `# nosec B608` waivers.
"""

from __future__ import annotations

import json
from typing import Any

from jfastframework.sql import safe_identifier
from jfastframework.vectors.base import Chunk, SearchHit


class PgVectorStore:
    def __init__(self, engine: Any, *, table: str, dimensions: int) -> None:
        self._engine = engine
        self._table = safe_identifier(table, kind="vector table")
        self._dimensions = dimensions

    async def ensure_schema(self) -> None:
        from sqlalchemy import text

        async with self._engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._table} (
                        id          BIGSERIAL PRIMARY KEY,
                        tenant_id   TEXT,
                        document_id TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        content     TEXT NOT NULL,
                        metadata    JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        embedding   VECTOR({self._dimensions}) NOT NULL,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (document_id, chunk_index)
                    )
                    """
                )
            )
            # IVFFlat only helps once there is data; harmless to create empty.
            await conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {self._table}_embedding_idx "
                    f"ON {self._table} USING ivfflat (embedding vector_cosine_ops) "
                    f"WITH (lists = 100)"
                )
            )
            await conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {self._table}_tenant_idx "
                    f"ON {self._table} (tenant_id)"
                )
            )

    async def upsert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int:
        from sqlalchemy import text

        if not chunks:
            return 0
        document_ids = {chunk.document_id for chunk in chunks}

        async with self._engine.begin() as conn:
            for document_id in document_ids:
                await conn.execute(
                    text(f"DELETE FROM {self._table} WHERE document_id = :doc"),  # nosec B608
                    {"doc": document_id},
                )
            for chunk, embedding in zip(chunks, embeddings, strict=True):
                await conn.execute(
                    text(
                        f"INSERT INTO {self._table} "  # nosec B608
                        f"(tenant_id, document_id, chunk_index, content, metadata, embedding) "
                        f"VALUES (:tenant, :doc, :idx, :content, "
                        f"CAST(:meta AS jsonb), CAST(:emb AS vector))"
                    ),
                    {
                        "tenant": chunk.tenant_id,
                        "doc": chunk.document_id,
                        "idx": chunk.chunk_index,
                        "content": chunk.content,
                        "meta": json.dumps(chunk.metadata),
                        "emb": str(embedding),
                    },
                )
        return len(chunks)

    async def search(
        self,
        embedding: list[float],
        *,
        limit: int = 5,
        tenant_id: str | None = None,
    ) -> list[SearchHit]:
        from sqlalchemy import text

        clause = "WHERE tenant_id = :tenant" if tenant_id is not None else ""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"SELECT document_id, chunk_index, content, metadata, "  # nosec B608
                    f"1 - (embedding <=> CAST(:emb AS vector)) AS score "
                    f"FROM {self._table} {clause} "
                    f"ORDER BY embedding <=> CAST(:emb AS vector) LIMIT :limit"
                ),
                {"emb": str(embedding), "limit": limit, "tenant": tenant_id},
            )
            return [
                SearchHit(
                    document_id=row["document_id"],
                    chunk_index=row["chunk_index"],
                    content=row["content"],
                    score=float(row["score"]),
                    metadata=row["metadata"] or {},
                )
                for row in result.mappings()
            ]

    async def delete_document(self, document_id: str) -> None:
        from sqlalchemy import text

        async with self._engine.begin() as conn:
            await conn.execute(
                text(f"DELETE FROM {self._table} WHERE document_id = :doc"),  # nosec B608
                {"doc": document_id},
            )

    async def health(self) -> tuple[bool, str]:
        from sqlalchemy import text

        try:
            async with self._engine.connect() as conn:
                await conn.execute(text(f"SELECT 1 FROM {self._table} LIMIT 1"))  # nosec B608
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return False, f"pgvector table unreachable: {exc}"
        return True, f"pgvector table {self._table} reachable"
