"""JWT authentication.

    [plugins]
    enabled = ["observability", "auth"]

    [plugin.auth]
    mode = "jwks"                                   # jwks | public_key | secret
    jwks_url = "https://id.example.com/.well-known/jwks.json"
    issuer = "https://id.example.com/"
    audience = "billing"
    algorithms = ["RS256"]

Then guard a route:

    from jfastframework.auth import Principal, require_scopes

    @router.post("/invoices")
    async def create(caller: Principal = Depends(require_scopes("invoices:write"))):
        ...

**What this plugin does and does not do.** It verifies tokens, and it can mint
them. It does not know who your users are: there is no login endpoint, because
checking a password against your user table is your application's job, not a
framework's. `auth.issuer` is provided for you to call from your own login
route.

The security decisions are documented where they are made -- see
``jfastframework.auth.tokens`` for algorithm pinning and claim verification,
``jfastframework.auth.jwks`` for key rotation, and
``jfastframework.auth.store`` for revocation.

Requires: ``pip install jfastframework[auth]``
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import SettingsConfigDict
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from jfastframework.auth.jwks import JWKSClient, JWKSError
from jfastframework.auth.principal import Principal, principal_var
from jfastframework.auth.store import MemoryTokenStore, RedisTokenStore, TokenStore
from jfastframework.auth.tokens import (
    SYMMETRIC_ALGORITHMS,
    TokenClaims,
    TokenError,
    issue,
    verify,
)
from jfastframework.errors import ForbiddenError, PluginError, UnauthorizedError
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.context import AppContext

logger = logging.getLogger("jfast.auth")

MODES = ("jwks", "public_key", "secret")


class AuthSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_AUTH_", env_file=".env", extra="ignore")

    # jwks       : fetch public keys from an issuer. The right default for
    #              more than one service -- no shared secret to leak.
    # public_key : a pinned PEM. Same trust model, no network dependency.
    # secret     : HMAC. Simple, and every holder can also *mint* tokens, so
    #              it does not belong between services that do not trust
    #              each other equally.
    mode: str = "jwks"
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"])

    jwks_url: str = ""
    jwks_cache_seconds: int = 3600
    public_key: str = ""
    secret: SecretStr | None = None

    issuer: str = ""
    audience: str = ""
    # Clock skew allowance. Seconds, not minutes: a generous leeway is extra
    # life for a stolen token.
    leeway: int = 30

    scope_claim: str = "scope"
    roles_claim: str = "roles"
    tenant_claim: str = "tenant_id"

    # Issuance, for a service that owns its own login.
    issue_tokens: bool = False
    access_lifetime_minutes: int = 15
    refresh_lifetime_days: int = 30
    # Mounts /auth/refresh and /auth/logout. Not /auth/login: this plugin has
    # no user store and will not pretend otherwise.
    mount_router: bool = True
    prefix: str = "/auth"

    # Reject a token whose jti has been revoked. Costs one store lookup per
    # authenticated request.
    check_revocation: bool = True


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


class TokenIssuer:
    """Mints tokens. Call it from your own login route.

    Refresh tokens are rotated: every refresh returns a new one and invalidates
    the one presented. Presenting a used refresh token means either a client
    retry or a replayed stolen token -- indistinguishable from here, so the
    whole family is revoked and the user has to log in again. Losing a session
    is a much smaller cost than not noticing a theft.
    """

    def __init__(
        self,
        *,
        key: Any,
        algorithm: str,
        issuer: str,
        audience: str,
        access_lifetime: timedelta,
        refresh_lifetime: timedelta,
        store: TokenStore,
        claims: TokenClaims,
    ) -> None:
        self._key = key
        self._algorithm = algorithm
        self._issuer = issuer
        self._audience = audience
        self._access_lifetime = access_lifetime
        self._refresh_lifetime = refresh_lifetime
        self._store = store
        self._claims = claims

    async def issue_pair(
        self,
        subject: str,
        *,
        scopes: list[str] | None = None,
        roles: list[str] | None = None,
        tenant_id: str | None = None,
        family: str | None = None,
    ) -> TokenPair:
        access, _, expires_at = issue(
            subject,
            key=self._key,
            algorithm=self._algorithm,
            lifetime=self._access_lifetime,
            audience=self._audience or None,
            issuer=self._issuer or None,
            scopes=scopes,
            roles=roles,
            tenant_id=tenant_id,
            token_type="access",
            claims=self._claims,
        )

        # The family ties every refresh in a session together, so detecting one
        # replay can end all of them.
        refresh_family = family or subject
        refresh, refresh_id, _ = issue(
            subject,
            key=self._key,
            algorithm=self._algorithm,
            lifetime=self._refresh_lifetime,
            audience=self._audience or None,
            issuer=self._issuer or None,
            tenant_id=tenant_id,
            token_type="refresh",
            extra={"fam": refresh_family},
            claims=self._claims,
        )
        await self._store.remember_refresh(
            refresh_id,
            family=refresh_family,
            ttl=int(self._refresh_lifetime.total_seconds()),
        )

        return TokenPair(
            access_token=access,
            refresh_token=refresh,
            expires_in=int((expires_at - datetime.now(UTC)).total_seconds()),
        )

    async def rotate(self, principal: Principal) -> TokenPair:
        family = str(principal.claims.get("fam", principal.subject))
        token_id = principal.token_id

        if principal.claims.get("typ") != "refresh":
            raise UnauthorizedError("an access token cannot be used to refresh")
        if token_id is None:
            raise UnauthorizedError("refresh token has no id")
        if await self._store.is_family_revoked(family):
            raise UnauthorizedError("this session has been revoked")

        consumed = await self._store.rotate_refresh(
            token_id, family=family, ttl=int(self._refresh_lifetime.total_seconds())
        )
        if not consumed:
            # Replay. Kill the family rather than guess which holder is real.
            await self._store.revoke_family(family, ttl=int(self._refresh_lifetime.total_seconds()))
            logger.warning(
                "refresh token replay; family revoked",
                extra={"subject": principal.subject, "family": family},
            )
            raise UnauthorizedError("this session has been revoked")

        return await self.issue_pair(
            principal.subject,
            tenant_id=principal.tenant_id,
            family=family,
        )


class AuthMiddleware(BaseHTTPMiddleware):
    """Verify a bearer token when one is present, and never reject here.

    Rejection is the dependency's job: a public endpoint must keep working
    with a bad token in the header, and an authenticated one must fail with
    the right status. Doing it here would make every route private.

    Verifying anyway means the access log and every log line inside the
    request carry the caller's identity, including on public routes.
    """

    def __init__(self, app: Any, *, plugin: AuthPlugin) -> None:
        super().__init__(app)
        self._plugin = plugin

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        header = request.headers.get("Authorization", "")
        principal: Principal | None = None

        if header.lower().startswith("bearer "):
            try:
                principal = await self._plugin.verify_token(header[7:].strip())
            except (TokenError, JWKSError) as exc:
                # The reason belongs in the log, never in the response: an
                # attacker learning *why* a token failed gets free
                # reconnaissance.
                logger.info("token rejected", extra={"reason": str(exc)})

        token = principal_var.set(principal)
        request.state.principal = principal
        if principal is not None and principal.tenant_id:
            # A signed claim beats the X-Tenant-ID header the observability
            # plugin would otherwise trust. Anyone can send that header.
            request.state.tenant_id = principal.tenant_id
            from jfastframework.plugins.builtin.observability import tenant_id_var

            tenant_token = tenant_id_var.set(principal.tenant_id)
        else:
            tenant_token = None

        try:
            response: Response = await call_next(request)
            return response
        finally:
            principal_var.reset(token)
            if tenant_token is not None:
                from jfastframework.plugins.builtin.observability import tenant_id_var

                tenant_id_var.reset(tenant_token)


# -- dependencies -------------------------------------------------------


def optional_auth(request: Request) -> Principal | None:
    """The caller, or None. For routes that behave differently when signed in."""
    principal: Principal | None = getattr(request.state, "principal", None)
    return principal


def require_auth(request: Request) -> Principal:
    """A verified caller, or 401."""
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise UnauthorizedError("authentication required")
    return principal


def require_scopes(*scopes: str) -> Callable[[Request], Principal]:
    """Require every listed scope, or 403.

    401 means "I do not know who you are"; 403 means "I do, and you may not".
    Collapsing them into one status makes debugging a permissions problem
    guesswork.
    """

    def dependency(request: Request) -> Principal:
        principal = require_auth(request)
        if not principal.has_scope(*scopes):
            missing = sorted(set(scopes) - principal.scopes)
            raise ForbiddenError(f"missing scope(s): {', '.join(missing)}")
        return principal

    return dependency


def require_roles(*roles: str) -> Callable[[Request], Principal]:
    """Require any one of these roles, or 403."""

    def dependency(request: Request) -> Principal:
        principal = require_auth(request)
        if not principal.has_any_role(*roles):
            raise ForbiddenError(f"requires one of: {', '.join(sorted(roles))}")
        return principal

    return dependency


# -- plugin -------------------------------------------------------------


class AuthPlugin(Plugin):
    meta = PluginMeta(
        name="auth",
        version="0.1.0",
        description="JWT verification with JWKS rotation, scopes, and revocation.",
        after=("observability", "cache"),
        provides=("auth", "auth.issuer", "auth.store"),
        default_enabled=False,
        extra="jfastframework[auth]",
    )
    Settings = AuthSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._jwks: JWKSClient | None = None
        self._key: Any = None
        self._store: TokenStore | None = None
        self._issuer: TokenIssuer | None = None
        self._claims = TokenClaims()

    # -- configuration -------------------------------------------------

    def _validate(self) -> None:
        settings: AuthSettings = self.settings
        if settings.mode not in MODES:
            raise PluginError(f"auth mode must be one of {', '.join(MODES)}, not {settings.mode!r}")

        symmetric = set(settings.algorithms) & SYMMETRIC_ALGORITHMS
        asymmetric = set(settings.algorithms) - SYMMETRIC_ALGORITHMS

        if symmetric and asymmetric:
            # Allowing both is the algorithm-confusion attack, configured in.
            raise PluginError(
                "auth.algorithms mixes symmetric and asymmetric algorithms "
                f"({', '.join(sorted(settings.algorithms))}). Allowing both lets a token "
                "signed with the public key as an HMAC secret verify. Pick one family."
            )

        if settings.mode == "jwks":
            if not settings.jwks_url:
                raise PluginError('auth mode "jwks" needs [plugin.auth] jwks_url.')
            if symmetric:
                raise PluginError('auth mode "jwks" cannot verify HMAC algorithms.')
        elif settings.mode == "public_key":
            if not settings.public_key:
                raise PluginError('auth mode "public_key" needs [plugin.auth] public_key.')
            if symmetric:
                raise PluginError('auth mode "public_key" cannot verify HMAC algorithms.')
        elif settings.mode == "secret":
            if settings.secret is None:
                raise PluginError('auth mode "secret" needs JFAST_AUTH_SECRET.')
            if asymmetric:
                raise PluginError('auth mode "secret" cannot verify asymmetric algorithms.')

        if settings.issue_tokens and settings.mode == "jwks":
            # Minting requires a private key; JWKS publishes public ones.
            raise PluginError(
                'auth cannot issue tokens in "jwks" mode: minting needs a private key. '
                'Use mode = "secret" for a single service, or issue from your identity service.'
            )

        if not settings.audience:
            logger.warning(
                "auth has no audience configured: a token minted for any other service "
                "of the same issuer will be accepted here"
            )

    # -- lifecycle -----------------------------------------------------

    def register(self, ctx: AppContext) -> None:
        settings: AuthSettings = self.settings
        self._validate()
        self._claims = TokenClaims(
            scopes=settings.scope_claim,
            roles=settings.roles_claim,
            tenant=settings.tenant_claim,
        )

        if settings.mode == "jwks":
            self._jwks = JWKSClient(
                url=settings.jwks_url, cache_seconds=settings.jwks_cache_seconds
            )
        elif settings.mode == "public_key":
            self._key = settings.public_key
        else:
            assert settings.secret is not None
            self._key = settings.secret.get_secret_value()

        if ctx.has("cache.client"):
            self._store = RedisTokenStore(ctx.require("cache.client"))
        else:
            self._store = MemoryTokenStore()
            ctx.logger.warning(
                "auth is using an in-memory token store: a logout applies to this "
                "process only. Enable the 'cache' plugin for shared revocation."
            )

        ctx.provide("auth", self)
        ctx.provide("auth.store", self._store)

        if settings.issue_tokens:
            self._issuer = TokenIssuer(
                key=self._key,
                algorithm=settings.algorithms[0],
                issuer=settings.issuer,
                audience=settings.audience,
                access_lifetime=timedelta(minutes=settings.access_lifetime_minutes),
                refresh_lifetime=timedelta(days=settings.refresh_lifetime_days),
                store=self._store,
                claims=self._claims,
            )
            ctx.provide("auth.issuer", self._issuer)

        ctx.app.add_middleware(AuthMiddleware, plugin=self)

        if settings.mount_router:
            ctx.app.include_router(self._build_router(), prefix=settings.prefix, tags=["auth"])

    async def verify_token(self, token: str) -> Principal:
        """Verify a bearer token. Raises TokenError or JWKSError."""
        import jwt

        settings: AuthSettings = self.settings
        key: Any = self._key

        if self._jwks is not None:
            try:
                header = jwt.get_unverified_header(token)
            except jwt.InvalidTokenError as exc:
                raise TokenError(f"malformed token header: {exc}") from exc
            # The header's `kid` only selects which *public* key to try. The
            # algorithm still comes from configuration, never from the token.
            jwk = await self._jwks.key_for(header.get("kid"))
            key = jwt.PyJWK(jwk).key

        principal = verify(
            token,
            key=key,
            algorithms=list(settings.algorithms),
            audience=settings.audience or None,
            issuer=settings.issuer or None,
            leeway=settings.leeway,
            claims=self._claims,
        )

        if settings.check_revocation and principal.token_id and self._store is not None:
            if await self._store.is_revoked(principal.token_id):
                raise TokenError("token has been revoked")
            family = principal.claims.get("fam")
            if family and await self._store.is_family_revoked(str(family)):
                raise TokenError("session has been revoked")

        return principal

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/me", summary="The verified caller")
        async def me(caller: Principal = Depends(require_auth)) -> dict[str, Any]:
            # Never the token, never the raw claims: just identity and rights.
            return caller.describe()

        @router.post("/logout", status_code=204, summary="Revoke this token")
        async def logout(caller: Principal = Depends(require_auth)) -> None:
            if self._store is None or caller.token_id is None:
                return
            remaining = 0
            if caller.expires_at is not None:
                remaining = int((caller.expires_at - datetime.now(UTC)).total_seconds())
            # Revoke the family too, or the refresh token issued alongside
            # this one quietly mints a new session.
            await self._store.revoke(caller.token_id, ttl=max(remaining, 1))
            family = caller.claims.get("fam", caller.subject)
            await self._store.revoke_family(
                str(family),
                ttl=int(timedelta(days=self.settings.refresh_lifetime_days).total_seconds()),
            )

        if self.settings.issue_tokens:

            @router.post("/refresh", response_model=TokenPair, summary="Rotate a refresh token")
            async def refresh(payload: RefreshRequest) -> TokenPair:
                if self._issuer is None:
                    raise UnauthorizedError("token issuance is disabled")
                try:
                    principal = await self.verify_token(payload.refresh_token)
                except (TokenError, JWKSError) as exc:
                    logger.info("refresh rejected", extra={"reason": str(exc)})
                    raise UnauthorizedError("invalid refresh token") from exc
                return await self._issuer.rotate(principal)

        return router

    async def health(self, ctx: AppContext) -> HealthReport:
        settings: AuthSettings = self.settings
        meta: dict[str, Any] = {
            "mode": settings.mode,
            "algorithms": list(settings.algorithms),
            "audience": settings.audience or None,
            "issues_tokens": settings.issue_tokens,
        }

        if self._jwks is not None:
            healthy, detail = await self._jwks.health()
            if not healthy:
                return HealthReport.fail(detail, **meta)
            meta["key_ids"] = list(self._jwks.key_ids)

        if self._store is not None:
            store_ok, store_detail = await self._store.health()
            if not store_ok:
                # A degraded store means revocation is weaker than intended.
                # The service still authenticates, so this is not critical.
                return HealthReport.fail(store_detail, critical=False, **meta)

        return HealthReport.ok("auth configured", **meta)
