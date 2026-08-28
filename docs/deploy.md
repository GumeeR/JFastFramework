# Deployment

Deployment artifacts are **generated from the plugin graph**, not maintained by
hand. Disable a plugin and its container disappears from the next generated
compose file. That is the whole idea: infrastructure cannot drift away from
what the application actually loads.

## Port blocks

A service owns ten consecutive ports starting at its base port
(`[app].port`). Each plugin declares an offset inside the block.

| Offset | Convention | Declared by |
| --- | --- | --- |
| +0 | HTTP API | the service |
| +1 | PostgreSQL | `database` |
| +2 | PgBouncer | *(planned)* |
| +3 | Redis | `cache` |
| +4 | MongoDB | `mongo` |
| +5 | Prometheus | `metrics` |
| +6 | Grafana | `metrics` |
| +7 | Qdrant HTTP | `qdrant` |
| +8 | Qdrant gRPC | `qdrant` |

A service on 8010 gets PostgreSQL on 8011, Redis on 8013 and Qdrant on 8017.
An offset of 10 or more is rejected — it would collide with the next service's
block. A container needing more than one port declares `extra_ports`.

## Generate compose

```bash
jfast deploy compose --stdout          # inspect
jfast deploy compose -o docker-compose.yml
jfast deploy compose --base-port 8020  # override the block base
```

Given:

```toml
[app]
name = "billing"
port = 8010

[plugins]
enabled = ["observability", "metrics", "database", "cache"]
```

you get an `api` service plus `postgres` (8011) and `redis` (8013), with named
volumes, healthchecks, and `depends_on` conditions wired to those healthchecks
so the API does not start against a database that is not accepting connections
yet.

Prometheus and Grafana are opt-in even when `metrics` is on — most services
scrape from a central Prometheus rather than running their own:

```toml
[plugin.metrics]
include_infra = true
```

## Generate a Dockerfile

```bash
jfast deploy dockerfile
```

Produces a slim image that runs as a non-root user (uid 10001), installs
dependencies in a cached layer before copying source, and ships a `HEALTHCHECK`
hitting `/health`.

Running containers as root is a finding in every security review, and fixing it
after the fact means rebuilding image layers across the fleet. The generated
Dockerfile starts correct.

## Secrets

The generated compose references environment variables rather than inlining
values:

```yaml
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}
```

The `:?` form makes compose fail with a clear message instead of silently
starting PostgreSQL with an empty password.

Never put a DSN, key or password in `jfast.toml` — it is committed. Settings
that hold secrets are typed `SecretStr`, which also keeps them out of
`jfast describe` and `/info`.

## The generated file is generated

`docker-compose.generated.yml` carries a header saying so. Hand edits are lost
on the next run. If you need something the generator does not emit:

- infrastructure a plugin owns → add it to that plugin's `infra()`
- something specific to one environment → a compose override file
  (`docker-compose.override.yml`), which compose merges automatically

## Kubernetes

Not implemented. Phase 4 in [PLAN.md](../PLAN.md) covers Deployment, Service,
ConfigMap, Secret and HPA generation from the same `infra()` declarations. Until
then, write manifests by hand — and do not assume the port-block convention
maps cleanly onto cluster networking, where services address each other by DNS
name and port collisions are not a concern.

## Checklist before production

- [ ] `JFAST_ENV=prod` — this alone disables `/info`
- [ ] `JFAST_DEBUG=false` — otherwise exception messages reach clients
- [ ] Every secret from the environment, none from `jfast.toml`
- [ ] `/ready` wired to the orchestrator's readiness probe, `/health` to
      liveness — not the other way round
- [ ] RAG `auto_migrate` off; schema managed by Alembic
- [ ] Image scanned; container runs as non-root (the generated one does)
