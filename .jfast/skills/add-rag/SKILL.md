---
name: add-rag
description: Enable semantic search over pgvector or Qdrant, with a local
  Ollama embedder or a custom one.
when_to_use: The user wants semantic search, a knowledge base, "chat with my
  documents", or retrieval to ground an LLM answer.
when_not_to_use: Exact-match or keyword search is enough — use a PostgreSQL
  index or full-text search. RAG is not a substitute for a WHERE clause.
---

## What you get

`POST /rag/documents` (ingest), `POST /rag/search`, `DELETE /rag/documents/{id}`,
plus `rag.store` and `rag.embedder` providers for code that needs them
directly.

## Known limits — say these before building on it

- Chunking is fixed-size with overlap. Fine for prose, poor for code and tables.
- No reranking. Top-k by cosine similarity only.
- No hybrid search. Pure vector, no BM25 blend.
- `auto_migrate` runs DDL at startup. Fine in development; move to migrations
  before production.

If the use case needs better retrieval than that, say so rather than shipping
this and calling it done.

## Step 1: choose the store

| | pgvector (default) | Qdrant |
| --- | --- | --- |
| Operational cost | none — the database you already run | a second service to run and back up |
| Scale | comfortable to a few million chunks | far beyond |
| Filtering | tenant id, plus whatever SQL you write | rich indexed payload filters |

**Start with pgvector.** Move to Qdrant when you hit a wall you can name —
filter complexity, index build time, memory. "It might scale better" is not
that wall.

## Step 2: enable the plugins

pgvector:

```bash
pip install -e ".[rag,db]"
```

```toml
[plugins]
enabled = ["observability", "database", "rag"]

[plugin.rag]
store = "pgvector"
collection = "rag_chunks"
dimensions = 768
chunk_size = 1000
chunk_overlap = 150
top_k = 5
```

Qdrant:

```bash
pip install -e ".[rag,qdrant]"
```

```toml
[plugins]
enabled = ["observability", "qdrant", "rag"]

[plugin.rag]
store = "qdrant"
collection = "rag_chunks"
dimensions = 768
```

Getting this pairing wrong fails at startup with the fix in the message, not at
the first search:

```
rag store 'qdrant' needs the 'qdrant' plugin. Add "qdrant" to [plugins].enabled.
```

## Step 3: match dimensions to the model

The mistake that costs an afternoon. `nomic-embed-text` is 768;
`mxbai-embed-large` is 1024. A mismatch fails at insert, and changing it later
means re-embedding everything already ingested. Decide before the first ingest.

```bash
ollama pull nomic-embed-text
curl -s localhost:11434/api/tags | jq -r '.models[].name'
```

For pgvector, the image must carry the extension — `pgvector/pgvector:pg16`,
which the `database` plugin already defaults to.

## Step 4: ingest and search

```bash
curl -X POST localhost:8000/rag/documents \
  -H 'content-type: application/json' \
  -d '{"document_id":"doc-1","content":"...","metadata":{"source":"manual"}}'

curl -X POST localhost:8000/rag/search \
  -H 'content-type: application/json' \
  -d '{"query":"how do refunds work","limit":5}'
```

Re-ingesting a document replaces its chunks in both stores — pgvector deletes
by `document_id`, Qdrant derives point ids deterministically from
`(document_id, chunk_index)`.

## Swapping the embedder

Implement the protocol — a `dimensions` attribute and an async
`embed(texts) -> list[list[float]]` — then:

```toml
[plugin.rag]
embedder = "myapp.embeddings:OpenAIEmbedder"
```

A custom store works the same way (`store = "myapp.stores:MyStore"`); it is
constructed with the `AppContext`, so it can pull its client from the provider
registry.

## Multi-tenancy

Pass `tenant_id` on ingest and search. Both stores filter on it and index it.
Until row-level security lands (PLAN.md phase 2) that filter is a convention:
a caller that omits `tenant_id` searches across tenants. If the data is
tenant-sensitive, set the value server-side from the JWT — never from the
request body.

## Verification

```bash
jfast doctor
curl -s localhost:8000/ready | jq '.checks.rag'
```

Then ingest one known document and search for a phrase inside it. If the top
result is not that document, the embedder or the dimensions are wrong — check
those before touching `chunk_size`.
