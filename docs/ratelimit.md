# Rate limiting

A login endpoint with no rate limit is a credential-stuffing target. This plugin
is the limit, and it lives in the application rather than at the edge.

```toml
[plugins]
enabled = ["observability", "cache", "auth", "ratelimit"]

[plugin.ratelimit]
limit = 100
window = 60.0
```

Off unless you enable it. A limit is a policy decision with a wrong answer for
somebody, and a service that starts refusing traffic because a default turned
itself on is worse than one with no limit.

## This is the application-layer limiter, not a replacement for the edge

An edge limiter — Caddy, an ingress, Cloudflare — sees addresses and paths. This
one sees the things an edge cannot:

- **which tenant** is calling, because the tenancy plugin already resolved it;
- **which subject**, because the auth plugin already verified the token;
- **what the endpoint costs**, because a vector search is not a health check.

The two are complementary and you want both. The edge stops a volumetric flood
before it reaches a worker; this stops one customer from spending another's
quota, and it is the only layer that can tell those apart. Note the boundary in
the other direction: enforcement is a per-route dependency, so a request that
matches **no** route — a 404 flood, a scan — is not limited here. That is the
edge's job.

## Algorithm: token bucket, evaluated in Redis

A fixed window lets a caller spend a full quota at 11:59:59 and another at
12:00:00, so the real ceiling is twice the configured one across every boundary.
A token bucket has no boundary. It refills continuously at `limit / window`
tokens per second, and its burst size is an explicit knob instead of an accident
of where the clock happens to tick. It also costs one hash per identity, where a
sliding-window log costs one entry per request.

| Setting | Meaning |
| --- | --- |
| `limit` | Requests per window. Also the refill rate: `limit / window` per second. |
| `window` | Seconds to refill an empty bucket completely. |
| `burst` | Bucket size. Defaults to `limit` — a full window may arrive at once. |

### Why a Lua script and not `GET` then `SET`

Two requests that read the same counter before either writes **both pass**. That
is not a rare interleaving; it is precisely the load a limiter exists for, and a
limiter that leaks under load is decoration. Redis runs a script to completion
with nothing else interleaved, so the read, the decision and the write are one
indivisible step:

```lua
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000

local stored = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(stored[1])
-- refill, then spend, then store -- all before anyone else runs
tokens = math.min(burst, tokens + (now - ts) * rate)
if tokens >= cost then allowed = 1; tokens = tokens - cost end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
```

Two details that are not incidental:

- **The clock is Redis's own** (`TIME`), not the caller's. Workers on different
  hosts have different clocks, and a bucket shared between them must not refill
  at a rate that depends on which worker asked. That makes the script
  non-deterministic, which requires effect replication — the default since Redis
  5.
- **Floats cross as strings.** Redis truncates a Lua number to an integer on the
  way out, which would round every fractional token down to nothing.

The bucket is in Redis and not in the process because an in-process counter is
wrong the moment there are two workers, and both `jfast dev` and the generated
compose run more than one. The connection comes from the `cache` plugin — hence
`requires = ["cache"]` — rather than a second pool to the same server.

## What the limit is keyed on

`key_sources` is an order of specificity, and the first source that answers wins.

| Source | Key | Notes |
| --- | --- | --- |
| `principal` | `sub:<subject>` | From the verified token. The right answer when there is one. |
| `api_key` | `key:<sha256 prefix>` | **Off unless `api_key_header` is set.** |
| `tenant` | `tenant:<id>` | Anonymous traffic, limited per tenant. |
| `ip` | `ip:<address>` | The fallback. |

Keyed on IP, one office behind one NAT is one caller. Keyed on the subject, a
compromised account cannot spend its neighbours' quota. Falling back to the
tenant limits anonymous traffic per tenant rather than globally — which also
means one abusive caller can spend that tenant's anonymous quota. Drop `tenant`
from `key_sources` where that is the wrong trade.

`api_key` is off by default on purpose. An unverified header is chosen by the
caller, and a caller who can choose their own bucket has no limit at all. Set
`api_key_header` only where something upstream has already verified the key.

### The IP, behind a proxy

Behind a load balancer every request carries the balancer's address, so a
limiter keyed on the socket peer gives the entire internet one shared bucket.

This plugin **never parses `X-Forwarded-For` itself**. The header is
attacker-written until something that knows the proxy topology has walked it
from the right and stopped at the first untrusted hop; a limiter that trusts it
raw hands every caller a free bucket per forged hop. The contract is one
attribute:

```python
request.state.client_ip = "203.0.113.7"   # written by the trusted-proxy middleware
```

Until that is set, the socket peer is used, and a request arriving with
forwarding headers that nobody resolved logs a warning once, saying that every
client behind the proxy is sharing one bucket.

## Per-route cost

One global limit is either too tight for the cheap routes or too loose for the
expensive ones. Override it per route with a dependency, the same shape
`require_scopes` uses:

```python
from fastapi import Depends
from jfastframework.plugins.builtin.ratelimit import rate_limit

@router.post("/search", dependencies=[Depends(rate_limit(10, 60))])
async def search(): ...

# One combined budget across a family of expensive endpoints.
@router.post("/embed", dependencies=[Depends(rate_limit(30, 60, scope="vectors"))])
async def embed(): ...

# Or keep the shared budget and charge more for this one.
@router.post("/reindex", dependencies=[Depends(rate_limit(100, 60, cost=25))])
async def reindex(): ...
```

An override **replaces** the service-wide default on that route rather than
stacking with it, and it has its own bucket: spending the `/search` budget does
not touch the default one. Routes sharing a `scope` share a budget.

The default is installed on every API route at startup, which is when routers
have finished mounting. Routes added after the app has started do not get it.

## The response

`429`, with a problem document — the same `application/problem+json` shape every
other failure in a JFast service uses:

```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/problem+json
Retry-After: 12
RateLimit-Limit: 100
RateLimit-Remaining: 0
RateLimit-Reset: 47

{
  "type": "about:blank",
  "title": "Too Many Requests",
  "status": 429,
  "detail": "Rate limit of 100 request(s) per 60s exceeded.",
  "instance": "/search",
  "limit": 100,
  "remaining": 0,
  "retry_after": 12,
  "request_id": "01J..."
}
```

`RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset` are on successful
responses too, so a well-behaved client can slow down before it is refused.
`Reset` is the seconds until the bucket is full again; `Retry-After` is the
seconds until one more request would be allowed, and is never `0` — telling a
refused client to retry immediately is the one thing it must not do.

## When Redis is down: fail open

**The limiter allows the request, logs at `WARNING`, and reports the service
`degraded` in `/ready`.**

Failing closed turns a cache outage into a total outage: Redis blinks and every
endpoint in the fleet starts answering 429, including the ones that were never
near a limit. Failing open costs the limit for the duration of the outage. An
availability problem is traded for an abuse window, and the abuse window is the
cheaper of the two.

What makes that trade defensible is that it is not silent, because "silently" is
the entire objection to it:

```json
{
  "status": "degraded",
  "checks": {
    "ratelimit": {
      "healthy": false,
      "critical": false,
      "detail": "failing open: rate limit backend unreachable, no limit is being enforced",
      "meta": {"enforcing": false}
    }
  }
}
```

`/ready` still answers `200` — the check is not critical, because taking every
replica out of rotation is the outage this design exists to avoid — but it says
`degraded` and names the reason. Alert on it.

Set `fail_open = false` on a service where the limit matters more than the
endpoint does.

## Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `limit` | `100` | Requests per window. |
| `window` | `60.0` | Seconds for a full refill. |
| `burst` | `limit` | Bucket size. |
| `key_prefix` | app name | Redis key namespace. |
| `key_sources` | `["principal", "api_key", "tenant", "ip"]` | Order of specificity. |
| `api_key_header` | `""` | Off. Only set it where the key is verified upstream. |
| `fail_open` | `true` | See above. |
| `exempt_paths` | probes and docs | `/health`, `/ready`, `/info`, `/metrics`, `/docs`, `/redoc`, `/openapi.json` |

Probes are exempt by default and should stay that way: an orchestrator polling
`/ready` every two seconds must never be throttled out of the fleet.

Environment overrides use the `JFAST_RATELIMIT_` prefix — `JFAST_RATELIMIT_LIMIT=500`.
