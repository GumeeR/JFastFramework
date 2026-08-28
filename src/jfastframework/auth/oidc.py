"""Sign in with Google — and any other OIDC provider.

The auth plugin already verifies JWTs against a JWKS endpoint, and a Google ID
token is exactly that: a JWT signed by Google, with Google's keys published at
a well-known URL. So "log in with Google" needs almost no new machinery, just
the right issuer, audience and discovery document.

The flow, and who does what:

1. Your frontend sends the user to Google (:func:`authorization_url`).
2. Google redirects back with a `code`.
3. Your backend exchanges it for an **ID token** (:meth:`OIDCProvider.exchange`).
4. This module verifies that ID token.
5. **You** look the user up or create them, and mint *your own* token with
   `auth.issuer`.

Step 5 is yours on purpose. A Google ID token says "Google believes this is
person@example.com". It does not say what they may do in your system, it
expires on Google's schedule, and you cannot revoke it. Exchanging it for your
own token is what puts scopes, your tenant and your revocation back under your
control.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from jfastframework.auth.jwks import JWKSClient
from jfastframework.auth.principal import Principal
from jfastframework.auth.tokens import TokenError, verify


@dataclass(frozen=True)
class ProviderPreset:
    """Endpoints for a known provider, so nobody has to look them up."""

    name: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    default_scopes: tuple[str, ...] = ("openid", "email", "profile")
    algorithms: tuple[str, ...] = ("RS256",)


PRESETS: dict[str, ProviderPreset] = {
    "google": ProviderPreset(
        name="google",
        issuer="https://accounts.google.com",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
    ),
    "microsoft": ProviderPreset(
        name="microsoft",
        issuer="https://login.microsoftonline.com/common/v2.0",
        authorization_endpoint=("https://login.microsoftonline.com/common/oauth2/v2.0/authorize"),
        token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        jwks_uri="https://login.microsoftonline.com/common/discovery/v2.0/keys",
    ),
    "github": ProviderPreset(
        name="github",
        # GitHub is OAuth2, not OIDC: no ID token, so the userinfo call is
        # the only way to learn who signed in. Listed for completeness; it
        # does not go through verify_id_token().
        issuer="https://github.com",
        authorization_endpoint="https://github.com/login/oauth/authorize",
        token_endpoint="https://github.com/login/oauth/access_token",
        jwks_uri="",
        default_scopes=("read:user", "user:email"),
    ),
}


@dataclass
class OIDCIdentity:
    """Who the provider says this is. Not yet a user in your system."""

    subject: str
    email: str | None = None
    email_verified: bool = False
    name: str | None = None
    picture: str | None = None
    provider: str = ""
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def federated_id(self) -> str:
        """Stable identifier to store on your user row.

        Provider-qualified, because subject ids are only unique per provider,
        and because a user who later signs in with a different provider must
        not silently collide with someone else.
        """
        return f"{self.provider}:{self.subject}"


class OIDCProvider:
    """One configured provider."""

    def __init__(
        self,
        preset: ProviderPreset,
        *,
        client_id: str,
        client_secret: str = "",
        redirect_uri: str = "",
        scopes: tuple[str, ...] | None = None,
    ) -> None:
        self.preset = preset
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes or preset.default_scopes
        self._jwks = JWKSClient(url=preset.jwks_uri) if preset.jwks_uri else None

    # -- step 1 --------------------------------------------------------

    def authorization_url(
        self, *, state: str | None = None, nonce: str | None = None
    ) -> tuple[str, str, str]:
        """Where to send the user. Returns ``(url, state, nonce)``.

        Keep both values: `state` is checked on the callback (CSRF), and
        `nonce` is checked inside the ID token (replay). Generating them and
        then not verifying them is the same as not having them.
        """
        state = state or secrets.token_urlsafe(24)
        nonce = nonce or secrets.token_urlsafe(24)
        query = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            "nonce": nonce,
            # Ask for a refresh token and force the consent screen only when
            # you actually need offline access; both annoy users otherwise.
        }
        return f"{self.preset.authorization_endpoint}?{urlencode(query)}", state, nonce

    # -- step 3 --------------------------------------------------------

    async def exchange(self, code: str) -> dict[str, Any]:
        """Swap the authorization code for tokens. Server-side only.

        The client secret must never reach a browser. If your frontend is a
        SPA, this call belongs in your backend, which is where this runs.
        """
        import httpx

        if not self.client_secret:
            raise TokenError(f"{self.preset.name}: no client secret configured")

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                self.preset.token_endpoint,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"},
            )
        if response.status_code >= 400:
            # The provider's body often contains the client_id; log the code
            # and a short reason, not the whole payload.
            raise TokenError(f"{self.preset.name} token exchange failed ({response.status_code})")
        payload: dict[str, Any] = response.json()
        return payload

    # -- step 4 --------------------------------------------------------

    async def verify_id_token(self, id_token: str, *, nonce: str | None = None) -> OIDCIdentity:
        """Verify an ID token and return who it identifies.

        Audience is this client id, issuer is the provider. Both matter: an ID
        token minted for a *different* application of the same provider is a
        valid Google token, and accepting it lets anyone with their own Google
        app sign in as anybody here.
        """
        import jwt

        if self._jwks is None:
            raise TokenError(f"{self.preset.name} does not issue ID tokens")

        header = jwt.get_unverified_header(id_token)
        jwk = await self._jwks.key_for(header.get("kid"))

        principal: Principal = verify(
            id_token,
            key=jwt.PyJWK(jwk).key,
            algorithms=list(self.preset.algorithms),
            audience=self.client_id,
            issuer=self.preset.issuer,
        )

        if nonce is not None and principal.claims.get("nonce") != nonce:
            # Without this a captured ID token can be replayed into a fresh
            # login attempt.
            raise TokenError("ID token nonce does not match this login attempt")

        claims = principal.claims
        return OIDCIdentity(
            subject=principal.subject,
            email=claims.get("email"),
            # An unverified email must not be used to match an existing
            # account: it is an account-takeover primitive.
            email_verified=bool(claims.get("email_verified", False)),
            name=claims.get("name"),
            picture=claims.get("picture"),
            provider=self.preset.name,
            claims=claims,
        )

    async def health(self) -> tuple[bool, str]:
        if self._jwks is None:
            return True, f"{self.preset.name} (OAuth2, no ID tokens)"
        return await self._jwks.health()


def provider(
    name: str,
    *,
    client_id: str,
    client_secret: str = "",
    redirect_uri: str = "",
    issuer: str = "",
    jwks_uri: str = "",
    authorization_endpoint: str = "",
    token_endpoint: str = "",
) -> OIDCProvider:
    """A known provider by name, or a custom one from its endpoints."""
    preset = PRESETS.get(name)
    if preset is None:
        missing = [
            field_name
            for field_name, value in (
                ("issuer", issuer),
                ("jwks_uri", jwks_uri),
                ("authorization_endpoint", authorization_endpoint),
                ("token_endpoint", token_endpoint),
            )
            if not value
        ]
        if missing:
            raise TokenError(
                f"Unknown provider {name!r}. Either use one of "
                f"{', '.join(sorted(PRESETS))}, or supply: {', '.join(missing)}."
            )
        preset = ProviderPreset(
            name=name,
            issuer=issuer,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            jwks_uri=jwks_uri,
        )
    return OIDCProvider(
        preset,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )
