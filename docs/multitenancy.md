# Multi-tenancy

One deployment, many customers, each seeing only their own data.

The plugin answers one question — **which tenant is this request for?** — and
puts the answer on `request.state.tenant_id`, in every log line, and in the
repository base class. What it does *not* do is enforce isolation. That
distinction matters and is spelled out at the end of this page.

```toml
[plugins]
enabled = ["observability", "auth", "tenancy"]

[plugin.tenancy]
sources = ["token", "subdomain"]
base_domain = "app.example.com"
```

## Sources are an order of trust

The list is tried in order and the first hit wins. That order is the design:

| Source | Controlled by | Trust |
| --- | --- | --- |
| `token` | your identity provider, cryptographically | high |
| `subdomain` | your DNS and TLS | medium |
| `path` | the URL | low |
| `header` | whoever sent the request | **none** |

`header` exists because it is genuinely useful in development and in tests. It
is not in the default list, and enabling it in production logs a warning,
because `X-Tenant-ID: acme` is one `curl` away from another tenant's data.

A signed claim always outranks the hostname. Someone who points `acme.` at your
IP has not become Acme; someone holding a token your identity provider signed
for Acme has.

## Subdomains

`acme.app.example.com` with `base_domain = "app.example.com"` resolves to
`acme`. The rules, all of them deliberate:

- only the leftmost label, and only one — `a.b.app.example.com` is a mistake,
  not a tenant called `a.b`;
- the bare base domain is not a tenant;
- `www`, `api`, `app`, `admin`, `static`, `cdn`, `mail` and friends are
  reserved and never resolve to a tenant;
- the label must match `[a-z0-9][a-z0-9-]{0,62}` — the slug ends up in
  hostnames, log fields and SQL parameters, and must be safe in all three.

`base_domain` is required when `subdomain` is a source. Without it, every
hostname looks like a tenant, so the plugin refuses to start rather than
resolve nonsense.

### Caddy

```bash
jfast workspace caddy --hostname app.example.com --production --wildcard-tenants
```

That emits a `app.example.com, *.app.example.com` site block with on-demand
TLS, plus the global `ask` endpoint that gates it:

```
{
	on_demand_tls {
		ask http://api:8000/internal/tenant-exists
		interval 2m
		burst 5
	}
}
```

**The `ask` endpoint is not optional.** Caddy cannot get a wildcard certificate
from a wildcard *match*, so it issues one per hostname on first sight. Without
`ask`, anyone who points a DNS record at your server can make you request
certificates for it until Let's Encrypt rate-limits your domain.

You implement it. It answers `200` if the tenant exists and anything else if it
does not:

```python
@router.get("/internal/tenant-exists")
async def tenant_exists(domain: str) -> Response:
    slug = domain.split(".", 1)[0]
    if await tenants.exists(slug):
        return Response(status_code=200)
    return Response(status_code=404)
```

Keep it off the public router, and make it cheap — it runs on every new
hostname Caddy sees, including the ones probing you.

You also need a wildcard DNS record (`*.app.example.com`) pointing at the same
address.

## Requiring a tenant

```toml
[plugin.tenancy]
require_tenant = true
```

Any request that resolves to no tenant gets a `403` in problem+json. Health
checks, metrics, `/docs` and `/openapi.json` are exempt — a readiness probe has
no tenant and must not fail.

## Ordering, and why the token source works

The middleware runs **innermost**, after auth. This is not incidental: Starlette's
`add_middleware` puts a middleware outermost, which would run tenancy *before*
auth and leave the signed claim unreadable, because no principal exists that
early. The plugin appends instead, so every source — including the token —
is available when it resolves.

## What this is not

The resolved tenant reaches `BaseRepository`, so a query that forgets to filter
is still filtered by the repository. **That is a convention, not isolation.**

Any of these defeats it: raw SQL, a join through an unscoped table, a bug in a
repository method, a background job that runs without a request. The guarantee
you want is PostgreSQL row-level security, where the database refuses the read
regardless of what the query asked for. That is not generated yet — see
PLAN.md phase 2.

Until then, treat tenancy as defence in depth over correct queries, not as a
replacement for them.

## See also

- [Authentication](auth.md) — the `tenant_id` claim
- [Deployment](deploy.md) — Caddy as the edge
