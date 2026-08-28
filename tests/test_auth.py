"""Auth: what a token must prove, and the attacks the defaults refuse.

Most of these are negative tests. That is deliberate — the failure modes here
are the ones that do not look like failures: a token that verifies when it
should not still returns 200.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import APIRouter, Depends

from jfastframework.auth import MemoryTokenStore, Principal, TokenError, issue, verify
from jfastframework.auth.tokens import TokenClaims
from jfastframework.errors import PluginError
from jfastframework.plugins.builtin.auth import (
    AuthPlugin,
    TokenIssuer,
    optional_auth,
    require_auth,
    require_roles,
    require_scopes,
)
from jfastframework.testing import build_test_app, client_for

SECRET = "a-test-secret-long-enough-for-sha256-at-least-32-bytes"
ISSUER = "https://id.example.com/"
AUDIENCE = "billing"


def mint(**overrides: Any) -> str:
    params: dict[str, Any] = {
        "key": SECRET,
        "algorithm": "HS256",
        "lifetime": timedelta(minutes=5),
        "audience": AUDIENCE,
        "issuer": ISSUER,
    }
    params.update(overrides)
    subject = params.pop("subject", "user-1")
    token, _, _ = issue(subject, **params)
    return token


def check(token: str, **overrides: Any) -> Principal:
    params: dict[str, Any] = {
        "key": SECRET,
        "algorithms": ["HS256"],
        "audience": AUDIENCE,
        "issuer": ISSUER,
    }
    params.update(overrides)
    return verify(token, **params)


# -- the happy path -----------------------------------------------------


def test_a_valid_token_identifies_the_caller() -> None:
    token = mint(scopes=["invoices:read", "invoices:write"], roles=["admin"], tenant_id="acme")
    caller = check(token)

    assert caller.subject == "user-1"
    assert caller.has_scope("invoices:read")
    assert caller.has_any_role("admin")
    assert caller.tenant_id == "acme"
    assert caller.token_id is not None


def test_scopes_are_read_from_a_list_or_a_space_delimited_string() -> None:
    # Issuers disagree about this and both spellings are in the wild.
    import jwt

    payload = {
        "sub": "u",
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        "scope": ["a", "b"],
    }
    caller = check(jwt.encode(payload, SECRET, algorithm="HS256"), audience=None, issuer=None)
    assert caller.scopes == {"a", "b"}


def test_claim_names_are_configurable() -> None:
    claims = TokenClaims(scopes="permissions", roles="groups", tenant="org")
    token = mint(scopes=["x"], roles=["y"], tenant_id="z", claims=claims)
    caller = check(token, claims=claims)

    assert caller.scopes == {"x"} and caller.roles == {"y"} and caller.tenant_id == "z"


# -- the attacks --------------------------------------------------------


def test_an_unsigned_token_is_rejected() -> None:
    import jwt

    # `alg: none` is the oldest JWT attack. Passing an explicit algorithm
    # allow-list is what refuses it.
    payload = {
        "sub": "attacker",
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
    }
    unsigned = jwt.encode(payload, key="", algorithm="none")

    with pytest.raises(TokenError):
        check(unsigned, audience=None, issuer=None)


def test_a_token_signed_with_another_key_is_rejected() -> None:
    with pytest.raises(TokenError, match="invalid"):
        check(mint(key="another-secret-also-long-enough-for-sha256-here"))


def test_a_token_for_another_audience_is_rejected() -> None:
    # Without this, one compromised low-value service's token opens a
    # high-value one from the same issuer.
    with pytest.raises(TokenError, match="different audience"):
        check(mint(audience="some-other-service"))


def test_a_token_from_another_issuer_is_rejected() -> None:
    with pytest.raises(TokenError, match="unexpected issuer"):
        check(mint(issuer="https://evil.example.com/"))


def test_an_expired_token_is_rejected() -> None:
    with pytest.raises(TokenError, match="expired"):
        check(mint(lifetime=timedelta(seconds=-120)))


def test_the_expiry_leeway_is_seconds_not_minutes() -> None:
    # A token 20s past expiry passes with the default 30s leeway; one two
    # minutes past does not. A generous leeway is extra life for a stolen token.
    assert check(mint(lifetime=timedelta(seconds=-20))).subject == "user-1"
    with pytest.raises(TokenError):
        check(mint(lifetime=timedelta(seconds=-120)))


def test_a_token_without_a_subject_is_rejected() -> None:
    import jwt

    payload = {
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
    }
    with pytest.raises(TokenError, match="missing a required claim"):
        check(jwt.encode(payload, SECRET, algorithm="HS256"), audience=None, issuer=None)


def test_an_unsupported_algorithm_cannot_be_configured() -> None:
    with pytest.raises(TokenError, match="unsupported algorithm"):
        check(mint(), algorithms=["none"])


def test_reserved_claims_cannot_be_overridden_when_issuing() -> None:
    # Otherwise a caller passing extra={"sub": "admin"} widens its own token.
    with pytest.raises(TokenError, match="reserved claim"):
        mint(extra={"sub": "admin"})


def test_mixing_symmetric_and_asymmetric_algorithms_is_refused() -> None:
    plugin = AuthPlugin({"mode": "secret", "secret": SECRET, "algorithms": ["HS256", "RS256"]})
    # This configuration *is* the algorithm-confusion attack: a token signed
    # with the RSA public key as an HMAC secret would verify.
    with pytest.raises(PluginError, match="mixes symmetric and asymmetric"):
        plugin._validate()


def test_jwks_mode_refuses_hmac_algorithms() -> None:
    plugin = AuthPlugin({"mode": "jwks", "jwks_url": "https://x/jwks", "algorithms": ["HS256"]})
    with pytest.raises(PluginError, match="cannot verify HMAC"):
        plugin._validate()


def test_jwks_mode_refuses_to_issue_tokens() -> None:
    plugin = AuthPlugin({"mode": "jwks", "jwks_url": "https://x/jwks", "issue_tokens": True})
    with pytest.raises(PluginError, match="needs a private key"):
        plugin._validate()


def test_secret_mode_requires_a_secret() -> None:
    with pytest.raises(PluginError, match="JFAST_AUTH_SECRET"):
        AuthPlugin({"mode": "secret", "algorithms": ["HS256"]})._validate()


# -- the HTTP surface ---------------------------------------------------


def guarded_router() -> APIRouter:
    router = APIRouter()

    @router.get("/public")
    async def public(caller: Principal | None = Depends(optional_auth)) -> dict[str, Any]:
        return {"caller": caller.subject if caller else None}

    @router.get("/private")
    async def private(caller: Principal = Depends(require_auth)) -> dict[str, str]:
        return {"subject": caller.subject}

    @router.get("/write")
    async def write(
        caller: Principal = Depends(require_scopes("invoices:write")),
    ) -> dict[str, str]:
        return {"subject": caller.subject}

    @router.get("/admin")
    async def admin(caller: Principal = Depends(require_roles("admin"))) -> dict[str, str]:
        return {"subject": caller.subject}

    return router


def auth_app():  # type: ignore[no-untyped-def]
    return build_test_app(
        plugins=["auth"],
        app_name="billing",
        routers=[guarded_router()],
        raw={
            "plugin": {
                "auth": {
                    "mode": "secret",
                    "secret": SECRET,
                    "algorithms": ["HS256"],
                    "issuer": ISSUER,
                    "audience": AUDIENCE,
                }
            }
        },
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_a_public_route_works_without_a_token() -> None:
    async with client_for(auth_app()) as client:
        response = await client.get("/public")
    assert response.status_code == 200
    assert response.json() == {"caller": None}


async def test_a_public_route_still_works_with_a_bad_token() -> None:
    # The middleware must never reject: rejection is the dependency's job, or
    # every route becomes private.
    async with client_for(auth_app()) as client:
        response = await client.get("/public", headers=bearer("not-a-token"))
    assert response.status_code == 200


async def test_a_private_route_without_a_token_is_401() -> None:
    async with client_for(auth_app()) as client:
        response = await client.get("/private")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_a_private_route_with_a_valid_token_succeeds() -> None:
    async with client_for(auth_app()) as client:
        response = await client.get("/private", headers=bearer(mint()))
    assert response.json() == {"subject": "user-1"}


async def test_a_missing_scope_is_403_not_401() -> None:
    # 401 means "I do not know who you are"; 403 means "I do, and you may not".
    # Collapsing them makes every permissions bug guesswork.
    async with client_for(auth_app()) as client:
        response = await client.get("/write", headers=bearer(mint(scopes=["invoices:read"])))
    assert response.status_code == 403
    assert "invoices:write" in response.json()["detail"]


async def test_the_right_scope_passes() -> None:
    async with client_for(auth_app()) as client:
        response = await client.get("/write", headers=bearer(mint(scopes=["invoices:write"])))
    assert response.status_code == 200


async def test_roles_are_any_of_not_all_of() -> None:
    async with client_for(auth_app()) as client:
        allowed = await client.get("/admin", headers=bearer(mint(roles=["admin", "auditor"])))
        denied = await client.get("/admin", headers=bearer(mint(roles=["auditor"])))
    assert allowed.status_code == 200
    assert denied.status_code == 403


async def test_the_rejection_reason_does_not_reach_the_client() -> None:
    # Telling an attacker *why* their token failed is free reconnaissance.
    async with client_for(auth_app()) as client:
        response = await client.get("/private", headers=bearer(mint(issuer="https://evil/")))
    assert response.status_code == 401
    assert response.json()["detail"] == "authentication required"


async def test_the_tenant_comes_from_the_token_not_the_header() -> None:
    router = APIRouter()

    @router.get("/tenant")
    async def tenant(caller: Principal = Depends(require_auth)) -> dict[str, Any]:
        return {"tenant": caller.tenant_id}

    app = build_test_app(
        plugins=["auth"],
        routers=[router],
        raw={
            "plugin": {
                "auth": {
                    "mode": "secret",
                    "secret": SECRET,
                    "algorithms": ["HS256"],
                    "issuer": ISSUER,
                    "audience": AUDIENCE,
                }
            }
        },
    )
    async with client_for(app) as client:
        response = await client.get(
            "/tenant",
            headers={**bearer(mint(tenant_id="acme")), "X-Tenant-ID": "attacker-corp"},
        )
    # Anyone can send X-Tenant-ID. Only the issuer can sign a claim.
    assert response.json() == {"tenant": "acme"}


# -- refresh rotation ---------------------------------------------------


def make_issuer(store: MemoryTokenStore) -> TokenIssuer:
    return TokenIssuer(
        key=SECRET,
        algorithm="HS256",
        issuer=ISSUER,
        audience=AUDIENCE,
        access_lifetime=timedelta(minutes=15),
        refresh_lifetime=timedelta(days=30),
        store=store,
        claims=TokenClaims(),
    )


async def test_a_refresh_returns_a_new_pair() -> None:
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1", scopes=["a"], tenant_id="acme")

    principal = check(pair.refresh_token)
    rotated = await issuer.rotate(principal)

    assert rotated.refresh_token != pair.refresh_token
    assert check(rotated.access_token).subject == "user-1"


async def test_an_access_token_cannot_be_used_to_refresh() -> None:
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1")

    from jfastframework.errors import UnauthorizedError

    with pytest.raises(UnauthorizedError, match="cannot be used to refresh"):
        await issuer.rotate(check(pair.access_token))


async def test_replaying_a_refresh_token_revokes_the_whole_session() -> None:
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1")
    first = check(pair.refresh_token)

    await issuer.rotate(first)

    from jfastframework.errors import UnauthorizedError

    # A used refresh token presented again is either a retry or a theft, and
    # they are indistinguishable. Killing the family is the safe answer.
    with pytest.raises(UnauthorizedError, match="revoked"):
        await issuer.rotate(first)

    assert await store.is_family_revoked("user-1")


async def test_a_rotated_token_from_a_revoked_family_is_refused() -> None:
    store = MemoryTokenStore()
    issuer = make_issuer(store)
    pair = await issuer.issue_pair("user-1")
    second = await issuer.rotate(check(pair.refresh_token))

    await store.revoke_family("user-1", ttl=3600)

    from jfastframework.errors import UnauthorizedError

    with pytest.raises(UnauthorizedError, match="revoked"):
        await issuer.rotate(check(second.refresh_token))


# -- revocation ---------------------------------------------------------


async def test_a_revoked_token_stops_working() -> None:
    token = mint()
    app = auth_app()
    plugin = next(p for p in app.state.plugins if p.meta.name == "auth")

    async with client_for(app) as client:
        assert (await client.get("/private", headers=bearer(token))).status_code == 200
        await client.post("/auth/logout", headers=bearer(token))
        after = await client.get("/private", headers=bearer(token))

    assert after.status_code == 401
    assert plugin is not None


async def test_the_in_memory_store_reports_itself_as_not_shared() -> None:
    # A logout that silently applies to one replica is worse than one that
    # says it does.
    healthy, detail = await MemoryTokenStore().health()
    assert healthy is False
    assert "other replicas" in detail


async def test_me_returns_identity_and_never_the_token() -> None:
    async with client_for(auth_app()) as client:
        response = await client.get(
            "/auth/me", headers=bearer(mint(scopes=["a"], tenant_id="acme"))
        )
    body = response.json()
    assert body["subject"] == "user-1"
    assert body["tenant_id"] == "acme"
    assert "claims" not in body and "token" not in body
