# Choosing datastores

Every datastore is a plugin. Enable what the service needs, and the client, the
health check and the container all arrive together.

```toml
[plugins]
enabled = ["observability", "database", "cache", "qdrant", "mongo"]
```

| Plugin | Store | Provides | Extra | Port offset |
| --- | --- | --- | --- | --- |
| `database` | PostgreSQL (+pgvector) | `db.engine`, `db.sessionmaker` | `[db]` | +1 |
| `cache` | Redis | `cache`, `cache.client` | `[cache]` | +3 |
| `mongo` | MongoDB | `mongo.client`, `mongo.db` | `[mongo]` | +4 |
| `qdrant` | Qdrant | `qdrant.client` | `[qdrant]` | +7 (HTTP), +8 (gRPC) |

Running more than one is normal. Relational data with foreign keys belongs in
PostgreSQL; chat histories and scraped payloads are happier in Mongo. The
mistake is adopting a second store before the first one stops being enough —
each one is another thing to back up, monitor and restore at 3am.

---

## Vector search: pgvector or Qdrant

The `rag` plugin talks to a `VectorStore` protocol, so the backend is one line
of config.

```toml
[plugins]
enabled = ["observability", "database", "rag"]

[plugin.rag]
store = "pgvector"       # the default
collection = "rag_chunks"
dimensions = 768
```

Switching to Qdrant:

```toml
[plugins]
enabled = ["observability", "qdrant", "rag"]

[plugin.rag]
store = "qdrant"
```

Nothing else changes. Same endpoints, same `SearchHit` shape, same scores —
each store normalises to cosine similarity in [0, 1] so callers never have to
know whether the backend returned a distance or a similarity.

### Which one

| | pgvector | Qdrant |
| --- | --- | --- |
| Operational cost | none — it is the database you already run | a second service to run and back up |
| Scale | comfortable to a few million chunks | far beyond that |
| Filtering | tenant id, and whatever SQL you write | rich payload filters, indexed |
| Quantisation | no | yes |

**Start with pgvector.** Move to Qdrant when you hit a specific wall you can
name — filter complexity, index build time, memory. "It might scale better" is
not that wall.

### Picking the wrong one fails loudly

Choosing `pgvector` without the `database` plugin, or `qdrant` without the
`qdrant` plugin, raises at startup with the fix in the message:

```
rag store 'qdrant' needs the 'qdrant' plugin. Add "qdrant" to [plugins].enabled.
```

Not at the first search, in production, on a Friday.

---

## A custom store

Implement the protocol — `ensure_schema`, `upsert`, `search`,
`delete_document`, `health` — and point the config at it. The class is
constructed with the `AppContext`, so it can pull whatever it needs out of the
provider registry:

```python
from jfastframework.vectors import Chunk, SearchHit


class WeaviateStore:
    def __init__(self, ctx):
        self._client = ctx.require("weaviate.client")

    async def ensure_schema(self) -> None: ...
    async def upsert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int: ...
    async def search(self, embedding, *, limit=5, tenant_id=None) -> list[SearchHit]: ...
    async def delete_document(self, document_id: str) -> None: ...
    async def health(self) -> tuple[bool, str]: ...
```

```toml
[plugin.rag]
store = "myapp.stores:WeaviateStore"
```

The same escape hatch exists for embedders (`embedder = "myapp:OpenAIEmbedder"` —
anything with `dimensions` and an async `embed`).

---

## Embedding dimensions

The mistake that costs an afternoon: `dimensions` must match the embedding
model. `nomic-embed-text` is 768; `mxbai-embed-large` is 1024. A mismatch fails
at insert, and changing it later means re-embedding every document you have
already ingested. Decide it before the first ingest.

---

## Deployment

Enabled datastore plugins contribute their containers to the generated compose
file automatically:

```bash
jfast deploy compose --stdout
```

Disable `cache` and the Redis container is gone from the next generation. That
is the point of deriving infrastructure from the plugin graph rather than
maintaining it alongside.

Secrets stay in the environment. The generated compose references them with
compose's fail-fast form, so a missing password stops the stack instead of
starting PostgreSQL wide open:

```yaml
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}
```
