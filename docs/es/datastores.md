# Elegir datastores

Cada datastore es un plugin. Habilita lo que el servicio necesita, y el
cliente, el health check y el contenedor llegan todos juntos.

```toml
[plugins]
enabled = ["observability", "database", "cache", "qdrant", "mongo"]
```

| Plugin | Store | Provee | Extra | Offset de puerto |
| --- | --- | --- | --- | --- |
| `database` | PostgreSQL (+pgvector) | `db.engine`, `db.sessionmaker` | `[db]` | +1 |
| `cache` | Redis | `cache`, `cache.client` | `[cache]` | +3 |
| `mongo` | MongoDB | `mongo.client`, `mongo.db` | `[mongo]` | +4 |
| `qdrant` | Qdrant | `qdrant.client` | `[qdrant]` | +7 (HTTP), +8 (gRPC) |

Correr más de uno es normal. Los datos relacionales con foreign keys van en
PostgreSQL; los historiales de chat y los payloads scrapeados están más
cómodos en Mongo. El error es adoptar un segundo store antes de que el primero
deje de alcanzar — cada uno es una cosa más que respaldar, monitorear y
restaurar a las 3am.

---

## Búsqueda vectorial: pgvector o Qdrant

El plugin `rag` habla contra un protocolo `VectorStore`, así que el backend es
una línea de config.

```toml
[plugins]
enabled = ["observability", "database", "rag"]

[plugin.rag]
store = "pgvector"       # the default
collection = "rag_chunks"
dimensions = 768
```

Cambiar a Qdrant:

```toml
[plugins]
enabled = ["observability", "qdrant", "rag"]

[plugin.rag]
store = "qdrant"
```

No cambia nada más. Los mismos endpoints, la misma forma de `SearchHit`, los
mismos scores — cada store normaliza a similitud coseno en [0, 1], así que
quien llama nunca tiene que saber si el backend devolvió una distancia o una
similitud.

### Cuál

| | pgvector | Qdrant |
| --- | --- | --- |
| Costo operativo | ninguno — es la base de datos que ya corres | un segundo servicio que correr y respaldar |
| Escala | cómodo hasta unos pocos millones de chunks | mucho más allá de eso |
| Filtrado | tenant id, y el SQL que escribas | filtros ricos sobre el payload, indexados |
| Cuantización | no | sí |

**Empieza con pgvector.** Pasa a Qdrant cuando choques con un muro específico
que puedas nombrar — complejidad de los filtros, tiempo de build del índice,
memoria. "Quizá escale mejor" no es ese muro.

### Elegir mal falla ruidosamente

Elegir `pgvector` sin el plugin `database`, o `qdrant` sin el plugin `qdrant`,
revienta al arrancar y con el arreglo en el mensaje:

```
rag store 'qdrant' needs the 'qdrant' plugin. Add "qdrant" to [plugins].enabled.
```

No en la primera búsqueda, en producción, un viernes.

---

## Un store propio

Implementa el protocolo — `ensure_schema`, `upsert`, `search`,
`delete_document`, `health` — y apunta la config ahí. La clase se construye
con el `AppContext`, así que puede sacar del registro de providers lo que
necesite:

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

La misma escotilla de escape existe para los embedders
(`embedder = "myapp:OpenAIEmbedder"` — cualquier cosa con `dimensions` y un
`embed` async).

---

## Dimensiones del embedding

El error que cuesta una tarde: `dimensions` tiene que coincidir con el modelo
de embedding. `nomic-embed-text` es 768; `mxbai-embed-large` es 1024. Si no
coinciden falla al insertar, y cambiarlo después significa volver a embeber
cada documento que ya ingestaste. Decídelo antes del primer ingest.

---

## Deploy

Los plugins de datastore habilitados aportan sus contenedores al archivo de
compose generado, automáticamente:

```bash
jfast deploy compose --stdout
```

Deshabilita `cache` y el contenedor de Redis desaparece de la siguiente
generación. Ese es el punto de derivar la infraestructura del grafo de plugins
en vez de mantenerla en paralelo.

Los secretos se quedan en el entorno. El compose generado los referencia con
la forma fail-fast de compose, así una contraseña faltante detiene el stack en
vez de arrancar PostgreSQL abierto de par en par:

```yaml
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}
```
