---
name: add-service
description: Add a service to a workspace — choosing its datastores, its port
  block, and whether the system now needs a gateway.
when_to_use: The user asks for a new service, a new microservice, a second
  backend, or "split this out".
when_not_to_use: The work fits inside an existing service as a module — use
  create-module. Splitting too early costs a network hop and a deploy target
  for nothing.
---

## Before generating: does this need to be a service?

A new service is justified when it has its **own** reason to exist:

- a different scaling profile (a worker pool, a heavy vector search);
- a different release cadence or a different team;
- a different datastore that nothing else should reach.

It is *not* justified by "this feels like a separate concern". That is a
module. Splitting early buys a network hop, a second deploy, and an integration
test you did not have to write.

If the answer is module, stop and use `create-module`.

## Step 1: make sure there is a workspace

```bash
jfast workspace list
```

If that errors, create one first — it is what allocates ports and decides when
a gateway is needed:

```bash
jfast workspace init <project-name>
```

## Step 2: choose the datastores

```bash
jfast plugins list --all
```

| Need | Plugin |
| --- | --- |
| Relational data, foreign keys, migrations | `database` |
| Cache, pub/sub, queue | `cache` |
| Document-shaped data with no schema | `mongo` |
| Vector search past what pgvector handles | `qdrant` |
| Semantic search / RAG | `rag` (+ `database` or `qdrant`) |

Default to `database` alone. Every extra store is another thing to back up,
monitor and restore at 3am.

## Step 3: generate

```bash
jfast new service billing --with database,cache
```

Or interactively, which asks the same questions:

```bash
jfast init
```

The port comes from the next free ten-port block. Do not pass `--port` unless
you have a reason — plugins claim offsets inside the block, and hand-picking
consecutive ports makes two services fight over the same database port.

## Step 4: notice the gateway

At the **second** backend, a gateway is generated automatically and the
frontends' `VITE_API_URL` should be repointed at it:

```bash
jfast workspace env
```

If you added a service later, refresh the routes:

```bash
jfast workspace gateway --force
```

Never hand-edit `[[plugin.gateway.routes]]`. The workspace file is the source
of truth.

## Step 5: bring it up

```bash
cd billing
pip install -r requirements.txt
cp .env.example .env          # fill in the secrets
alembic revision --autogenerate -m "initial"
alembic upgrade head
uvicorn main:app --reload --port <port> --no-proxy-headers
```

## Verification

```bash
jfast doctor                       # config resolves, plugins import
jfast describe --text              # plugins, providers, infra
curl -s localhost:<port>/ready | jq
jfast workspace list               # port block, no collisions
```

If the workspace has a gateway, also check it routes to the new service:

```bash
curl -s localhost:<gateway-port>/gateway/routes | jq -r '.routes[].prefix'
```

## Common mistakes

- Creating a service for what is a module.
- Passing `--port` and landing inside another service's block.
- Adding `qdrant` "for later". Add it when pgvector actually stops being
  enough, and be able to say why.
- Enabling `rag` without a store — the plugin refuses at startup and names the
  missing plugin, but the generator should have got it right.
- Editing the gateway's routes by hand, then losing them on the next
  regeneration.
