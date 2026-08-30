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
| +2 | Kafka | `events` |
| +3 | Redis | `cache` |
| +4 | MongoDB | `mongo` |
| +5 | Prometheus | `metrics` |
| +6 | Grafana / RabbitMQ / MinIO | `metrics`, `queue`, `storage` |
| +7 | Qdrant HTTP | `qdrant` |
| +8 | Qdrant gRPC | `qdrant` |
| +9 | gRPC | the service |

A service on 8010 gets PostgreSQL on 8011, Redis on 8013 and Qdrant on 8017.
An offset of 10 or more is rejected — it would collide with the next service's
block. A container needing more than one port declares `extra_ports`.

Three plugins share +6 because all three are opt-in and no service is expected
to run all of them. If yours does, move one: every offset above is a setting
(`[plugin.queue] rabbitmq_port_offset`, and so on).

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

### Shared memory on PostgreSQL

The generated PostgreSQL container gets `shm_size: 1gb`. Docker's default is
64 MB, and `/dev/shm` is where a parallel query keeps its working memory — so
past the table size at which the planner starts parallelising, that query fails
with `could not resize shared memory segment`. A 500 on exactly the queries
that matter and on no others, which is why it reads as random until somebody
correlates it with row counts.

Any plugin can ask for the same: `InfraService(..., shm_size="1gb")`.

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

### Workers

One uvicorn worker is one Python process on one core. A request spends most of
its life outside the database — serialising, validating, rendering — so a
service can be nowhere near its database's limit and still be saturated:
measured on a feed page, 6.5 ms in PostgreSQL against 79 ms end to end at
concurrency 16.

The entrypoint therefore derives a worker count when the container starts:

```
JFAST_WORKERS=4 docker run …    # explicit wins
                                # otherwise: the container's CPU quota, capped at 8
```

Derived at start rather than baked into the image, because the image does not
know how much CPU it will be given. `nproc` alone is the wrong answer — it
reports the *host's* cores, so a container limited to half a core would start
as many workers as the machine has. cgroup v2 publishes the real quota
(`/sys/fs/cgroup/cpu.max`), so that is read first and `nproc` is the fallback.
The cap is there because past a point the workers only compete for the same
core, and each one costs a full copy of the application's memory.

To pin a number into the image instead, `render_dockerfile(workers=4)`.

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

## Edge protections

Four things are on before you configure anything, because `jfast deploy
function` puts a service on Lambda with nothing in front of it and `uvicorn
main:app` on a laptop has nothing either.

| Setting | Default | Off with |
| --- | --- | --- |
| `max_body_bytes` | `2097152` (2 MiB) | `0` |
| `request_timeout` | `30.0` | `0` |
| `security_headers` | `true` | `false` |
| `trusted_proxies` | loopback + private ranges | `[]` |

TOML has no null, so `0` is how a config file says "unlimited". 2 MiB is far
above any JSON body the framework generates a handler for and far below what
it costs to buffer one; 30s is the generated gateway's own `[plugin.gateway]
timeout`, so the service gives up at the same moment the thing in front of it
does rather than holding a worker for a response nobody is waiting for.

`jfast new service --with storage` writes larger numbers into `jfast.toml`
(25 MiB, 120s) — an upload service needs both, and a slow mobile connection
sending 25 MiB will not finish the body inside 30 seconds.

### Trusted proxies

`X-Forwarded-For` is written by whoever sent the request, so it is only
believed when the peer is in `trusted_proxies`. The chain is then walked from
the **right**, stopping at the first hop that is not one of your proxies —
everything further left was appended by somebody with no claim on your trust,
the client included.

The default list is loopback plus the private ranges (`10/8`, `172.16/12`,
`192.168/16`, `fc00::/7`), which is where the edge sits in everything this
framework generates: Caddy on a compose bridge, an ingress on a pod network, a
sidecar on localhost. A request arriving from a public address is a client
talking to you directly, and its `X-Forwarded-For` is a suggestion.

Narrow it to the balancer's real range on any deployment where a client can
reach the service from inside the private network:

```toml
[app]
trusted_proxies = ["10.0.4.0/24"]     # just the ingress subnet
```

`["*"]` trusts every peer. That is the only workable answer on a platform
whose front end has no stable address, and it is wrong anywhere the service is
also reachable directly.

The resolved address replaces `scope["client"]`, so `request.client.host` is
already correct. Code that would rather be explicit reads
`request.state.client_ip`, or:

```python
from jfastframework.middleware import client_ip

key = f"ip:{client_ip(request)}"
```

`X-Forwarded-Proto` from a trusted peer sets `request.url.scheme` the same
way, which is what lets HSTS know the request really arrived over TLS.

### Security headers

Every response carries `X-Content-Type-Options`, `X-Frame-Options`,
`Referrer-Policy`, `Permissions-Policy` and a Content-Security-Policy. A
header the application set itself is never overwritten, so one route that
needs a looser policy sets its own and the rest of the service stays strict.

`X-Frame-Options: DENY` goes out **alongside** CSP `frame-ancestors 'none'`,
not instead of it. The CSP directive supersedes it in a current browser, but
it is ignored in a report-only policy and in the embedded WebViews and
IE-mode frames that are still the reason clickjacking gets reported at all.

The default policy is **enforced**, and it is written around what this
framework's own output loads:

```
default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com;
style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:;
connect-src 'self'; frame-ancestors 'none'; base-uri 'self';
form-action 'self'; object-src 'none'
```

`'unsafe-inline'` is in there because the output it must not break requires
it: both HTMX base templates ship an inline `htmx:responseError` handler and
the scaffolder leaves an existing copy alone, and FastAPI's `/docs` page is an
inline `SwaggerUIBundle` call. A policy that 500s the first page of a new
service gets switched off within the hour. What the default does buy is
everything that costs nothing: no framing, no injected `<base>`, no
off-origin form post, no plugin embed, and no off-origin `fetch` or `<img>`
to exfiltrate through.

Outside production the policy also allows `https://cdn.jsdelivr.net`,
`https://fonts.googleapis.com`, `https://fonts.gstatic.com` and
`https://fastapi.tiangolo.com` — the CDNs `/docs` and `/redoc` load from. They
drop out on their own when the OpenAPI schema closes, which is what
`JFAST_ENV=prod` does by default. The tighter policy arrives with the
environment rather than with an edit somebody has to remember.

To go further, in this order:

1. `[plugin.web] htmx_cdn = false` and vendor `htmx.min.js` into `static/`.
   That removes `https://unpkg.com` from what the policy has to allow.
2. Set `csp_report_only = true` with your own tighter `csp` and a
   `report-uri`, and watch what breaks for a release.
3. Set `csp` to that policy without `'unsafe-inline'` and turn report-only
   back off. Any inline `<script>` left in your templates has to become a
   file first.

### HSTS

Off in local and dev, a year in production, and withheld from any request that
did not arrive over HTTPS — a browser ignores HSTS over cleartext anyway
(RFC 6797), and a developer reading `curl -I http://localhost` should not see
the service claim a guarantee nothing is enforcing.

The gate is real because the mistake is not recoverable from the application
side: a browser remembers HSTS for the whole `max-age`, so switching it on by
accident poisons `http://localhost` for a year and no amount of clearing the
app's cache fixes it.

```toml
[app]
hsts_seconds = 600            # ask for it anywhere, briefly, to test
hsts_preload = false          # submitting to the preload list is ~irreversible
```

## Checklist before production

- [ ] `JFAST_ENV=prod` — this alone disables `/info`, closes `/docs` and
      `/openapi.json`, tightens the CSP and switches HSTS on
- [ ] `JFAST_DEBUG=false` — otherwise exception messages reach clients
- [ ] Every secret from the environment, none from `jfast.toml`
- [ ] `/ready` wired to the orchestrator's readiness probe, `/health` to
      liveness — not the other way round
- [ ] RAG `auto_migrate` off; schema managed by Alembic
- [ ] Image scanned; container runs as non-root (the generated one does)
- [ ] `trusted_proxies` narrowed to the balancer's real range, if a client can
      reach the service from inside the private network
- [ ] `max_body_bytes` and `request_timeout` sized for what this service
      actually accepts, not left at the framework's guess
