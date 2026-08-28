"""Social login: the checks that decide whether a Google token is yours.

An ID token from Google is a valid, correctly signed JWT for *somebody's*
application. The only things separating "a user signed in here" from "anyone
with a Google app can impersonate anyone here" are the audience check, the
issuer check, and the state/nonce pair.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from jfastframework.auth.oidc import (
    PRESETS,
    OIDCIdentity,
    OIDCProvider,
    ProviderPreset,
    provider,
)
from jfastframework.auth.tokens import TokenError, issue

SECRET = "a-test-secret-long-enough-for-sha256-at-least-32-bytes"
CLIENT_ID = "1234.apps.googleusercontent.com"
ISSUER = "https://accounts.google.com"


def fake_preset() -> ProviderPreset:
    """A provider we can mint tokens for: HS256 instead of Google's RS256."""
    return ProviderPreset(
        name="google",
        issuer=ISSUER,
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        algorithms=("HS256",),
    )


class StubJWKS:
    """Stands in for the network call to the provider's key endpoint."""

    async def key_for(self, kid: str | None) -> Any:
        return SECRET

    async def health(self) -> tuple[bool, str]:
        return True, "stub"


def stub_provider(**overrides: Any) -> OIDCProvider:
    client = OIDCProvider(
        fake_preset(),
        client_id=CLIENT_ID,
        client_secret="s3cret",
        redirect_uri="https://app.example.com/auth/google/callback",
        **overrides,
    )
    client._jwks = StubJWKS()  # type: ignore[assignment]
    return client


def id_token(**overrides: Any) -> str:
    params: dict[str, Any] = {
        "key": SECRET,
        "algorithm": "HS256",
        "lifetime": timedelta(minutes=5),
        "audience": CLIENT_ID,
        "issuer": ISSUER,
        "extra": {"email": "someone@example.com", "email_verified": True, "nonce": "N"},
    }
    params.update(overrides)
    encoded, _jti, _expires = issue("108165", **params)
    return encoded


@pytest.fixture(autouse=True)
def _patch_pyjwk(monkeypatch: pytest.MonkeyPatch) -> None:
    """PyJWK wraps a JWKS dict; the stub hands back a raw HMAC key."""
    import jwt

    class PassThrough:
        def __init__(self, value: Any) -> None:
            self.key = value

    monkeypatch.setattr(jwt, "PyJWK", PassThrough)


# -- presets ------------------------------------------------------------


def test_google_preset_is_configured() -> None:
    google = PRESETS["google"]
    assert google.issuer == "https://accounts.google.com"
    assert "openid" in google.default_scopes
    # RS256 only. Accepting HS256 from a provider whose key is public would
    # let anyone sign their own token with it.
    assert google.algorithms == ("RS256",)


def test_unknown_provider_needs_its_endpoints() -> None:
    with pytest.raises(TokenError, match="issuer"):
        provider("acme", client_id="x")


def test_unknown_provider_is_accepted_when_fully_described() -> None:
    client = provider(
        "acme",
        client_id="x",
        issuer="https://id.acme.com",
        jwks_uri="https://id.acme.com/jwks",
        authorization_endpoint="https://id.acme.com/authorize",
        token_endpoint="https://id.acme.com/token",
    )
    assert client.preset.issuer == "https://id.acme.com"


# -- step 1: the redirect ----------------------------------------------


def test_authorization_url_carries_state_and_nonce() -> None:
    url, state, nonce = stub_provider().authorization_url()
    query = parse_qs(urlparse(url).query)

    assert query["client_id"] == [CLIENT_ID]
    assert query["response_type"] == ["code"]
    assert query["state"] == [state]
    assert query["nonce"] == [nonce]
    # Guessable values would defeat both checks they exist for.
    assert len(state) >= 20
    assert len(nonce) >= 20


def test_each_login_gets_fresh_values() -> None:
    _, state_a, nonce_a = stub_provider().authorization_url()
    _, state_b, nonce_b = stub_provider().authorization_url()
    assert state_a != state_b
    assert nonce_a != nonce_b


# -- step 4: verifying the ID token ------------------------------------


async def test_valid_id_token_yields_an_identity() -> None:
    identity = await stub_provider().verify_id_token(id_token(), nonce="N")
    assert identity.subject == "108165"
    assert identity.email == "someone@example.com"
    assert identity.email_verified is True
    assert identity.provider == "google"


async def test_token_for_another_client_is_refused() -> None:
    """The attack this stops: sign in to my Google app, replay the token here."""
    token = id_token(audience="9999.apps.googleusercontent.com")
    with pytest.raises(TokenError):
        await stub_provider().verify_id_token(token, nonce="N")


async def test_token_from_another_issuer_is_refused() -> None:
    with pytest.raises(TokenError):
        await stub_provider().verify_id_token(id_token(issuer="https://evil.example"), nonce="N")


async def test_expired_token_is_refused() -> None:
    with pytest.raises(TokenError):
        await stub_provider().verify_id_token(id_token(lifetime=timedelta(minutes=-5)), nonce="N")


async def test_wrong_nonce_is_refused() -> None:
    """A captured ID token replayed into a fresh login attempt."""
    with pytest.raises(TokenError, match="nonce"):
        await stub_provider().verify_id_token(id_token(), nonce="a-different-nonce")


async def test_nonce_is_only_checked_when_one_is_expected() -> None:
    identity = await stub_provider().verify_id_token(id_token())
    assert identity.subject == "108165"


async def test_exchange_without_a_client_secret_fails_before_the_network() -> None:
    client = OIDCProvider(fake_preset(), client_id=CLIENT_ID)
    with pytest.raises(TokenError, match="client secret"):
        await client.exchange("some-code")


async def test_github_has_no_id_token() -> None:
    client = provider("github", client_id="x", client_secret="y")
    with pytest.raises(TokenError, match="does not issue ID tokens"):
        await client.verify_id_token("anything")


# -- identity -----------------------------------------------------------


def test_federated_id_is_provider_qualified() -> None:
    # Subject ids are unique per provider, not globally. Storing the bare
    # subject lets a GitHub user collide with a Google user.
    google = OIDCIdentity(subject="42", provider="google")
    github = OIDCIdentity(subject="42", provider="github")
    assert google.federated_id == "google:42"
    assert google.federated_id != github.federated_id


def test_unverified_email_is_reported_as_unverified() -> None:
    identity = OIDCIdentity(subject="1", email="victim@example.com", provider="google")
    # Matching an existing account on an unverified email is account takeover.
    assert identity.email_verified is False


# -- the mounted routes -------------------------------------------------


def social_app(**overrides: Any) -> Any:
    """An app whose google provider is the stub, so no network is involved."""
    from jfastframework.plugins.builtin.auth import AuthPlugin
    from jfastframework.testing import build_test_app

    config: dict[str, Any] = {
        "mode": "secret",
        "secret": SECRET,
        "algorithms": ["HS256"],
        "issuer": "https://id.example.com/",
        "audience": "billing",
        "providers": {
            "google": {
                "client_id": CLIENT_ID,
                "client_secret": "s3cret",
                "redirect_uri": "https://app.example.com/auth/google/callback",
            }
        },
    }
    config.update(overrides)
    app = build_test_app(
        plugins=["observability", "auth"],
        extra_plugins=[AuthPlugin],
        raw={"plugin": {"auth": config}},
    )
    # The plugin built a real Google client at register time (construction
    # touches no network). Swap it for the stub so verification is offline.
    plugin = app.state.jfast.require("auth")
    plugin._providers["google"] = stub_provider()
    return app, plugin


async def test_start_redirects_and_sets_the_cookie() -> None:
    from jfastframework.testing import client_for

    app, _ = social_app()
    async with client_for(app) as client:
        response = await client.get("/auth/google/start", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://accounts.google.com/")
    cookie = response.headers["set-cookie"]
    # Script must not be able to read it, and it must not ride along on a
    # cross-site POST.
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()


async def test_start_on_an_unknown_provider_is_a_404() -> None:
    from jfastframework.testing import client_for

    app, _ = social_app()
    async with client_for(app) as client:
        assert (await client.get("/auth/google2/start")).status_code == 404


async def test_callback_without_a_cookie_is_refused() -> None:
    from jfastframework.testing import client_for

    app, _ = social_app()
    async with client_for(app) as client:
        response = await client.get("/auth/google/callback?code=x&state=y")
    assert response.status_code == 401


async def test_callback_with_a_mismatched_state_is_refused() -> None:
    """Login CSRF: a code obtained in the attacker's browser, replayed here."""
    import json

    from jfastframework.testing import client_for

    app, _ = social_app()
    async with client_for(app) as client:
        client.cookies.set(
            "jfast_oidc",
            json.dumps({"state": "mine", "nonce": "N", "provider": "google"}),
            path="/auth",
        )
        response = await client.get("/auth/google/callback?code=x&state=theirs")
    assert response.status_code == 401


async def test_callback_without_a_handler_says_so() -> None:
    """A verified user and nowhere to put them is a configuration error."""
    import json

    from jfastframework.testing import client_for

    app, plugin = social_app()
    plugin._providers["google"].exchange = _stub_exchange  # type: ignore[method-assign]

    async with client_for(app) as client:
        client.cookies.set(
            "jfast_oidc",
            json.dumps({"state": "S", "nonce": "N", "provider": "google"}),
            path="/auth",
        )
        response = await client.get("/auth/google/callback?code=x&state=S")

    # A 500, not a cheerful 200: the login verified but there is nowhere to
    # log the user in to, and pretending otherwise hides the missing wiring.
    assert response.status_code == 500
    assert "on_identity" in response.json()["detail"]


async def test_successful_callback_reaches_the_handler() -> None:
    import json

    from fastapi import Request

    from jfastframework.testing import client_for

    app, plugin = social_app()
    plugin._providers["google"].exchange = _stub_exchange  # type: ignore[method-assign]
    seen: list[OIDCIdentity] = []

    @plugin.on_identity
    async def sign_in(identity: OIDCIdentity, request: Request) -> dict[str, str]:
        seen.append(identity)
        return {"user": identity.federated_id}

    async with client_for(app) as client:
        client.cookies.set(
            "jfast_oidc",
            json.dumps({"state": "S", "nonce": "N", "provider": "google"}),
            path="/auth",
        )
        response = await client.get("/auth/google/callback?code=x&state=S")

    assert response.status_code == 200
    assert response.json() == {"user": "google:108165"}
    assert seen[0].email == "someone@example.com"


async def _stub_exchange(code: str) -> dict[str, Any]:
    """Stands in for the round trip to the provider's token endpoint."""
    return {"id_token": id_token()}
