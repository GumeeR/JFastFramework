# Authentication

JWT verification, scopes, key rotation and revocation.

```toml
[plugins]
enabled = ["observability", "cache", "auth"]

[plugin.auth]
mode = "jwks"
jwks_url = "https://id.example.com/.well-known/jwks.json"
issuer = "https://id.example.com/"
audience = "billing"
algorithms = ["RS256"]
```

```python
from fastapi import Depends
from jfastframework.auth import Principal, require_scopes

@router.post("/invoices")
async def create(caller: Principal = Depends(require_scopes("invoices:write"))):
    ...
```

---

## What this does and does not do

It **verifies** tokens, and it can **mint** them. It has **no login endpoint**,
because checking a password against your user table is your application's job.
`auth.issuer` is provided for your own login route:

```python
issuer = request.app.state.jfast.require("auth.issuer")
pair = await issuer.issue_pair(user.id, scopes=user.scopes, tenant_id=user.tenant_id)
```

A framework that shipped a `/auth/login` would have to invent a user model,
a password hashing policy and a lockout strategy — and you would fight all
three.

---

## Choosing a mode

| Mode | Key | Use when |
| --- | --- | --- |
| `jwks` | fetched from the issuer | more than one service. **The default.** |
| `public_key` | a pinned PEM | one issuer, no network dependency wanted |
| `secret` | shared HMAC secret | a single service that also mints |

**`secret` does not belong between services.** Everything that can verify an
HMAC token can also mint one. A read-only reporting service holding that secret
can forge an admin token for the billing service. `jwks` and `public_key` split
that: the issuer holds the private key, everyone else holds a public one.

---

## The attacks the defaults refuse

These are checked in `tests/test_auth.py`, one test each.

**Algorithm confusion.** A service that trusts the token's own `alg` header can
be handed an HS256 token signed with the RSA *public key it publishes* as the
HMAC secret — and will verify it. The algorithms come from configuration and
are passed explicitly to the decoder. Configuring both families at once is
refused outright:

```
auth.algorithms mixes symmetric and asymmetric algorithms (HS256, RS256).
Allowing both lets a token signed with the public key as an HMAC secret verify.
Pick one family.
```

**`alg: none`.** Ruled out by the same allow-list. It is never in
`SUPPORTED_ALGORITHMS` and must not be added.

**Cross-service token reuse.** `aud` and `iss` are verified. Both are off by
default in most libraries, and without them a valid token for a *different*
service of the same issuer is accepted here — which is how a compromised
low-value service becomes access to a high-value one. Leaving `audience` empty
logs a warning at startup rather than silently accepting everything.

**A generous clock skew.** The leeway is 30 seconds. Five minutes of leeway is
five extra minutes of life for a stolen token.

**JWKS refresh amplification.** An unknown `kid` triggers a refresh — that is
how rotation is picked up — but at most once a minute. Without the floor, a
stream of forged `kid`s becomes a denial-of-service against your identity
provider.

**Leaking why a token failed.** The reason goes to the log; the client gets a
plain 401. Telling an attacker *which* check failed is free reconnaissance.

---

## Tenancy stops being forgeable

Before this plugin, `tenant_id` comes from the `X-Tenant-ID` header —
convenient in development, and settable by anyone with curl. With auth enabled
it comes from a **signed claim**, and the header is ignored.

That is the main security reason to turn this on, more than the login form.

---

## 401 versus 403

- **401** — I do not know who you are. No token, or an invalid one.
- **403** — I do, and you may not. Authenticated, missing a scope or role.

Collapsing them makes every permissions bug guesswork. `require_scopes` names
the missing scopes in the response, because that one is not a secret from a
caller who is already authenticated.

```python
require_auth                       # any verified caller
require_scopes("a", "b")           # all of these scopes
require_roles("admin", "owner")    # any one of these roles
optional_auth                      # Principal | None, for mixed routes
```

---

## Revocation

JWTs are stateless, which is the point and also the problem: a token is valid
until it expires and "log out" has nothing to act on. The answer is short
access-token lifetimes plus a small amount of state.

`POST /auth/logout` revokes the caller's `jti` **and its refresh family** —
otherwise the refresh token issued alongside it quietly mints a new session.

Revocation entries carry the token's own remaining lifetime as a TTL: past
expiry the signature check rejects it anyway, so keeping the entry longer only
grows the store forever.

With the `cache` plugin enabled the store is Redis and a logout applies to
every replica. Without it the store is a dict, and `/ready` says so:

```
in-memory token store: revocation does not survive a restart or reach other replicas
```

Non-critical — the service still authenticates — but visible, rather than
discovered from a support ticket.

---

## Refresh rotation, with reuse detection

Every refresh returns a new refresh token and invalidates the one presented.
Presenting a used one means either a client retry or a replayed stolen token,
and from the server those are indistinguishable — so the whole **family** is
revoked and the user logs in again.

Losing one session is a far smaller cost than not noticing a theft.

```python
pair = await issuer.issue_pair("user-1", scopes=["invoices:read"], tenant_id="acme")
# POST /auth/refresh {"refresh_token": ...} -> a new pair
```

The Redis store consumes a refresh token with `DELETE`, whose return value
makes the check atomic: two concurrent refreshes cannot both succeed.

---

## Key rotation

With `mode = "jwks"` rotation is a publish, not a redeploy. The issuer adds a
new key to its JWKS document and starts signing with it; services fetch the new
key on the first token carrying an unknown `kid`.

Keep the old key published until every token signed with it has expired.

If the JWKS endpoint is unreachable, cached keys keep working — a JWKS outage
must not take every service down — and `/ready` reports the staleness.

---

## Checklist before production

- [ ] `mode = "jwks"` or `public_key`, not `secret`, if more than one service
- [ ] `audience` set to this service, `issuer` set to your identity provider
- [ ] `cache` enabled, so revocation is shared across replicas
- [ ] Access-token lifetime in minutes, not hours
- [ ] `JFAST_AUTH_SECRET` (if used) is 32+ random bytes, from a secret manager
- [ ] Tokens never logged. `Principal.describe()` is the safe shape

## What is not here

- **OAuth2 / OIDC flows.** Verifying the resulting token is this plugin's job;
  running the authorization code dance is the identity provider's.
- **A user store, password hashing, MFA, lockout.** Application concerns.
- **mTLS or service-to-service identity.** Machine tokens work today; SPIFFE
  is not implemented.
- **Per-tenant key isolation.** One issuer, one key set.
