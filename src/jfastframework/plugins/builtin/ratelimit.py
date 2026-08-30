"""Application-layer rate limiting, backed by Redis.

    [plugin.ratelimit]
    limit = 100
    window = 60.0

    from jfastframework.plugins.builtin.ratelimit import rate_limit

    @router.post("/search", dependencies=[Depends(rate_limit(10, 60))])
    async def search(): ...

This is the limiter that knows things an edge limiter cannot: which tenant is
calling, which authenticated subject, and that a vector search costs a hundred
times a health check. It is **complementary to** an edge limiter (Caddy, an
ingress, Cloudflare), not a replacement for one -- the edge stops a flood before
it reaches a worker; this stops one customer from spending another's quota.

Enforcement is a FastAPI dependency, installed on every API route at startup and
overridable per route. That is deliberate rather than middleware: a dependency
runs *after* every middleware, so the limiter sees the principal the auth plugin
verified and the tenant the tenancy plugin resolved. Middleware would run before
both and could only ever key on an address. The cost of that choice is its
boundary -- a request that matches no route, or a plain Starlette route or Mount
with no dependency graph, is not limited here. That traffic is the edge
limiter's job.

Requires: ``pip install jfastframework[cache]`` -- the Redis client ships with
the cache extra, and this plugin borrows the cache plugin's connection rather
than opening a second one.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import Depends, Request, Response
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from jfastframework.errors import JFastError, PluginError, problem_response
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.routing import BaseRoute

    from jfastframework.context import AppContext

logger = logging.getLogger("jfast.ratelimit")

# Sources for the bucket key, most specific first. See ``resolve_identity``.
IDENTITY_SOURCES = ("principal", "api_key", "tenant", "ip")

# Where the trusted-proxy middleware publishes the real client address. This
# plugin never reads ``X-Forwarded-For`` itself: the header is attacker-written
# until something that knows the proxy topology has walked it, and a limiter
# that trusts it hands every caller a free bucket per forged hop.
CLIENT_IP_STATE_ATTR = "client_ip"

DEFAULT_EXEMPT_PATHS = [
    "/health",
    "/ready",
    "/info",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
]

# Token bucket, evaluated entirely inside Redis.
#
# Why a token bucket rather than a fixed window: a fixed window lets a caller
# spend a full quota at 11:59:59 and another at 12:00:00, so the real ceiling is
# twice the configured one across every boundary. A token bucket has no
# boundary -- it refills continuously -- and its burst size is an explicit knob
# instead of an accident of where the clock happens to tick. It also costs one
# hash per identity, where a sliding-window log costs one entry per request.
#
# Why a script rather than GET-then-SET from Python: two requests that read the
# same counter before either writes both pass, which is precisely the load a
# limiter exists for. Redis runs a script to completion with nothing else
# interleaved, so the read, the decision and the write are one indivisible step.
#
# The clock is Redis's own (``TIME``), not the caller's: workers on different
# hosts have different clocks, and a bucket shared between them must not refill
# at a rate that depends on which worker asked. ``TIME`` makes the script
# non-deterministic, which requires effect replication -- the default since
# Redis 5.
TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local burst = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000

local stored = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(stored[1])
local ts = tonumber(stored[2])
if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end

local elapsed = now - ts
if elapsed < 0 then
  elapsed = 0
end
tokens = math.min(burst, tokens + elapsed * rate)

local allowed = 0
if tokens >= cost then
  allowed = 1
  tokens = tokens - cost
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)

local retry = 0
if allowed == 0 then
  retry = (cost - tokens) / rate
end

-- Floats have to cross as strings: Redis truncates a Lua number to an integer
-- on the way out, which would round every fractional token down to nothing.
return {allowed, tostring(tokens), tostring(retry), tostring((burst - tokens) / rate)}
"""


@dataclass(frozen=True)
class RateLimitPolicy:
    """One limit: ``limit`` requests per ``window`` seconds, burstable to ``burst``."""

    limit: int
    window: float = 60.0
    # What one request spends. A vector search is not one health check.
    cost: int = 1
    # Bucket size. Defaults to ``limit``, i.e. a full window may arrive at once.
    burst: int | None = None
    # Bucket namespace. Separate scopes are separate buckets, which is what
    # makes a per-route limit independent of the service-wide default.
    scope: str = "default"

    def __post_init__(self) -> None:
        if self.limit <= 0 or self.window <= 0 or self.cost <= 0:
            raise ValueError("limit, window and cost must all be positive")

    @property
    def capacity(self) -> int:
        return self.burst if self.burst is not None else self.limit

    @property
    def rate(self) -> float:
        """Tokens per second."""
        return self.limit / self.window

    @property
    def ttl(self) -> int:
        """Seconds an idle bucket is kept: long enough to refill, then gone."""
        return math.ceil(self.capacity / self.rate) + 1


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    reset: int
    retry_after: int
    # True when the backend did not answer and the policy decided without it.
    degraded: bool = False

    def headers(self) -> dict[str, str]:
        values = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(self.remaining),
            "RateLimit-Reset": str(self.reset),
        }
        if not self.allowed:
            values["Retry-After"] = str(self.retry_after)
        return values


class RateLimitedError(JFastError):
    status_code = 429
    title = "Too Many Requests"

    def __init__(
        self, detail: str | None = None, *, headers: dict[str, str] | None = None, **extra: Any
    ) -> None:
        super().__init__(detail, **extra)
        # Kept off ``extra`` so they stay headers and do not leak into the body.
        self.headers = headers or {}


def _number(value: Any) -> float:
    if isinstance(value, bytes):
        value = value.decode()
    return float(value)


class RateLimiter:
    """Runs one policy against one identity. The only place a decision is made."""

    # How often a continuing outage is allowed to repeat itself. The first
    # failure of an episode always logs; without the throttle a dead Redis
    # writes one line per request and buries everything else in the log.
    WARN_INTERVAL = 10.0

    def __init__(self, client: Any, *, prefix: str = "", fail_open: bool = True) -> None:
        self._client = client
        self._prefix = prefix
        self._fail_open = fail_open
        # register_script reloads the script on NOSCRIPT, so a Redis that was
        # restarted under us does not turn into a permanent failure.
        self._script = client.register_script(TOKEN_BUCKET_LUA)
        self._degraded = False
        self._last_warned = 0.0

    @property
    def degraded(self) -> bool:
        """True while the backend is not answering and the limit is not real."""
        return self._degraded

    def key(self, policy: RateLimitPolicy, identity: str) -> str:
        return f"{self._prefix}rl:{policy.scope}:{identity}"

    async def check(self, policy: RateLimitPolicy, identity: str) -> Decision:
        try:
            raw = await self._script(
                keys=[self.key(policy, identity)],
                args=[policy.capacity, policy.rate, policy.cost, policy.ttl],
            )
        except Exception as exc:  # noqa: BLE001 - any client failure is an outage
            return self._on_outage(policy, exc)

        self._degraded = False
        allowed = bool(int(raw[0]))
        tokens = _number(raw[1])
        retry = _number(raw[2])
        reset = _number(raw[3])
        return Decision(
            allowed=allowed,
            limit=policy.limit,
            remaining=max(0, int(tokens)),
            reset=max(0, math.ceil(reset)),
            # Retry-After of 0 tells a client to retry immediately, which is
            # the one thing a refused client must not do.
            retry_after=0 if allowed else max(1, math.ceil(retry)),
        )

    def _on_outage(self, policy: RateLimitPolicy, exc: Exception) -> Decision:
        """Redis did not answer. Fail open, and make sure somebody finds out.

        Failing closed turns a cache outage into a total outage: Redis blinks
        and every endpoint in the fleet starts answering 429, including the ones
        that were never near a limit. Failing open costs the limit for the
        duration of the outage -- an availability problem is traded for an abuse
        window, and the abuse window is the cheaper of the two.

        What makes that trade defensible is that it is not silent, because
        "silently" is the entire objection to it. Every episode logs at WARNING,
        and ``/ready`` reports the service *degraded* for as long as it lasts,
        so an operator learns that limits are off from a probe rather than from
        a bill. Set ``fail_open = false`` on a service where the limit matters
        more than the endpoint does.
        """
        now = time.monotonic()
        first = not self._degraded
        self._degraded = True
        if first or now - self._last_warned >= self.WARN_INTERVAL:
            self._last_warned = now
            logger.warning(
                "rate limit backend unreachable (%s): %s. %s.",
                type(exc).__name__,
                exc,
                (
                    "Failing open -- no limit is being enforced"
                    if self._fail_open
                    else "Failing closed -- every limited request is being refused"
                ),
                extra={"policy_scope": policy.scope, "fail_open": self._fail_open},
            )

        if not self._fail_open:
            return Decision(
                allowed=False,
                limit=policy.limit,
                remaining=0,
                reset=policy.ttl,
                retry_after=max(1, math.ceil(1 / policy.rate)),
                degraded=True,
            )
        return Decision(
            allowed=True,
            limit=policy.limit,
            remaining=policy.limit,
            reset=policy.ttl,
            retry_after=0,
            degraded=True,
        )


# -- identity -----------------------------------------------------------


_warned_about_proxy = False


def client_ip(request: Request) -> str:
    """The caller's address, as resolved by the trusted-proxy middleware.

    Behind a load balancer every request carries the balancer's address, so a
    limiter keyed on the socket peer gives the entire internet one shared
    bucket. Resolving the real address means knowing which hops are yours,
    which is the trusted-proxy middleware's job and not this plugin's: it
    publishes the answer on ``request.state.client_ip`` and this reads it.

    Until that lands, the socket peer is the honest fallback -- and a request
    that arrives carrying forwarding headers nobody resolved is worth saying
    out loud once.
    """
    global _warned_about_proxy

    resolved = getattr(request.state, CLIENT_IP_STATE_ATTR, None)
    if resolved:
        return str(resolved)

    if not _warned_about_proxy and "x-forwarded-for" in request.headers:
        _warned_about_proxy = True
        logger.warning(
            "Request carries X-Forwarded-For but no trusted-proxy middleware resolved "
            "request.state.%s. Rate limiting by IP is keyed on the proxy's address, so "
            "every client behind it shares one bucket.",
            CLIENT_IP_STATE_ATTR,
        )

    return request.client.host if request.client else "unknown"


def resolve_identity(
    request: Request,
    *,
    sources: list[str],
    api_key_header: str = "",
) -> str:
    """Who to charge for this request. The first source that answers wins.

    The order is an order of specificity, and it matters: keyed on IP, one
    office behind one NAT is one caller; keyed on the subject, a compromised
    account cannot spend its neighbours' quota. Falling back to the tenant
    limits anonymous traffic per tenant rather than globally -- which also means
    one abusive caller can spend that tenant's anonymous quota. Drop ``tenant``
    from ``key_sources`` where that is the wrong trade.
    """
    for source in sources:
        if source == "principal":
            principal = getattr(request.state, "principal", None)
            if principal is not None and getattr(principal, "subject", None):
                return f"sub:{principal.subject}"
        elif source == "api_key":
            # Only sound once something upstream has verified the key: an
            # unverified header is chosen by the caller, and a caller who can
            # choose their own bucket has no limit. Off unless configured.
            if api_key_header:
                presented = request.headers.get(api_key_header)
                if presented:
                    digest = hashlib.sha256(presented.encode()).hexdigest()[:32]
                    return f"key:{digest}"
        elif source == "tenant":
            tenant = getattr(request.state, "tenant_id", None)
            if tenant:
                return f"tenant:{tenant}"
        elif source == "ip":
            return f"ip:{client_ip(request)}"
    return "ip:unknown"


# -- enforcement --------------------------------------------------------


class _Enforcer:
    """Limiter plus key policy. Published on ``app.state`` for the dependency."""

    def __init__(
        self, limiter: RateLimiter, *, sources: list[str], api_key_header: str = ""
    ) -> None:
        self.limiter = limiter
        self.sources = sources
        self.api_key_header = api_key_header

    async def apply(self, request: Request, policy: RateLimitPolicy) -> Decision:
        identity = resolve_identity(
            request, sources=self.sources, api_key_header=self.api_key_header
        )
        return await self.limiter.check(policy, identity)


def _enforcer_for(request: Request) -> _Enforcer | None:
    return getattr(request.app.state, "jfast_ratelimit", None)


def _is_exempt(path: str, exempt: tuple[str, ...]) -> bool:
    return any(path == entry or path.startswith(entry.rstrip("/") + "/") for entry in exempt)


class RouteLimit:
    """One policy, applied as a FastAPI dependency.

    The same class is both the service-wide default -- installed on every API
    route at startup -- and a per-route override built by ``rate_limit()``. A
    route that already carries one never gets the default installed on top, so
    an override replaces rather than stacks.
    """

    def __init__(self, policy: RateLimitPolicy, *, exempt: tuple[str, ...] = ()) -> None:
        self.policy = policy
        self.exempt = exempt

    async def __call__(self, request: Request, response: Response) -> None:
        if self.exempt and _is_exempt(request.url.path, self.exempt):
            return
        # One charge per bucket per request. ``install_default_limit`` writes
        # into two places that different FastAPI versions read from, and this
        # is what keeps a version that reads both from charging twice.
        charged: set[str] = request.scope.setdefault("jfast_ratelimit_charged", set())
        if self.policy.scope in charged:
            return
        charged.add(self.policy.scope)

        enforcer = _enforcer_for(request)
        if enforcer is None:
            return
        decision = await enforcer.apply(request, self.policy)
        headers = decision.headers()
        if not decision.allowed:
            raise RateLimitedError(
                f"Rate limit of {self.policy.limit} request(s) per "
                f"{self.policy.window:g}s exceeded.",
                headers=headers,
                limit=self.policy.limit,
                remaining=decision.remaining,
                retry_after=decision.retry_after,
            )
        response.headers.update(headers)

    def __repr__(self) -> str:
        return f"<RouteLimit {self.policy.limit}/{self.policy.window:g}s scope={self.policy.scope}>"


def rate_limit(
    limit: int,
    window: float = 60.0,
    *,
    cost: int = 1,
    burst: int | None = None,
    scope: str | None = None,
) -> RouteLimit:
    """A per-route limit, as a FastAPI dependency.

        @router.post("/search", dependencies=[Depends(rate_limit(10, 60))])

    Replaces the service-wide default on that route rather than stacking with
    it. ``scope`` names the bucket: routes sharing a scope share a budget, which
    is how a family of expensive endpoints gets one combined quota.
    """
    policy = RateLimitPolicy(
        limit=limit,
        window=window,
        cost=cost,
        burst=burst,
        scope=scope or f"route:{limit}/{window:g}",
    )
    return RouteLimit(policy)


def _iter_dependant_routes(routes: list[BaseRoute]) -> Iterator[Any]:
    """Every route with a dependency graph, however deeply it was included.

    FastAPI stopped flattening ``include_router`` into ``app.routes``: an
    included router is a lazy branch that keeps its own routes. Recursing
    through both that branch and a Starlette ``Mount`` covers old and new
    layouts without asking which one this is.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _iter_dependant_routes(included.routes)
            continue
        nested = getattr(route, "routes", None)
        if nested:
            yield from _iter_dependant_routes(nested)
            continue
        if getattr(route, "dependant", None) is not None:
            yield route


def _declares_limit(route: Any) -> bool:
    for depends in getattr(route, "dependencies", None) or ():
        if isinstance(getattr(depends, "dependency", None), RouteLimit):
            return True
    dependant = getattr(route, "dependant", None)
    return dependant is not None and any(
        isinstance(sub.call, RouteLimit) for sub in dependant.dependencies
    )


def install_default_limit(app: FastAPI, default: RouteLimit) -> int:
    """Put ``default`` on every API route that does not declare its own limit.

    Runs at startup rather than at register time, because routers are mounted
    after plugins register and there is nothing to walk before that. Idempotent:
    the check for an existing ``RouteLimit`` is also what stops a second call
    from installing a second copy.

    Both the declared ``dependencies`` and the solved ``dependant`` are written
    to, because which one is authoritative depends on the FastAPI version and on
    how the route was mounted: a route added straight to the app keeps the
    dependant it was built with, while an included router is re-solved from its
    declared dependencies on first use. ``RouteLimit`` charges once per request
    either way.
    """
    from fastapi.dependencies.utils import get_parameterless_sub_dependant

    marker = Depends(default)
    installed = 0
    for route in _iter_dependant_routes(list(app.routes)):
        if _declares_limit(route):
            continue
        declared = getattr(route, "dependencies", None)
        if isinstance(declared, list):
            declared.insert(0, marker)
        route.dependant.dependencies.insert(
            0,
            get_parameterless_sub_dependant(depends=marker, path=getattr(route, "path_format", "")),
        )
        installed += 1
    return installed


def _rate_limited_handler(request: Request, exc: Exception) -> Response:
    """Renders ``RateLimitedError`` with its headers attached.

    The kernel's ``JFastError`` handler produces the right body but drops the
    headers, and ``Retry-After`` on a 429 is not decoration -- it is the only
    thing telling a well-behaved client when to come back.
    """
    assert isinstance(exc, RateLimitedError)
    response = problem_response(exc, request)
    response.headers.update(exc.headers)
    return response


# -- plugin -------------------------------------------------------------


class RateLimitSettings(PluginSettings):
    model_config = SettingsConfigDict(
        env_prefix="JFAST_RATELIMIT_", env_file=".env", extra="ignore"
    )

    limit: int = 100
    window: float = 60.0
    burst: int | None = None
    key_prefix: str = ""
    # Ordered by specificity; the first that answers names the bucket.
    key_sources: list[str] = Field(default_factory=lambda: ["principal", "api_key", "tenant", "ip"])
    # Empty means the api_key source never fires. Only set this where the key
    # has already been verified upstream -- see ``resolve_identity``.
    api_key_header: str = ""
    # A backend outage lets requests through. The reasoning is in
    # ``RateLimiter._on_outage``.
    fail_open: bool = True
    # Probes and docs. An orchestrator polling /ready must never be throttled.
    exempt_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_EXEMPT_PATHS))


class RateLimitPlugin(Plugin):
    meta = PluginMeta(
        name="ratelimit",
        version="0.1.0",
        description="Redis token-bucket rate limiting, per subject, tenant or IP.",
        requires=("cache",),
        after=("auth", "tenancy"),
        provides=("ratelimit",),
        # A limit is a policy decision with a wrong answer for somebody, and a
        # service that starts refusing traffic because a default turned itself
        # on is worse than one with no limit. Opt in.
        default_enabled=False,
        extra="jfastframework[cache]",
        # Failing open means an unreachable Redis degrades the service rather
        # than breaking it. Taking every replica out of rotation over it would
        # be the outage this design exists to avoid.
        health_critical=False,
    )
    Settings = RateLimitSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._client: Any = None
        self._limiter: RateLimiter | None = None
        self._default: RouteLimit | None = None

    def register(self, ctx: AppContext) -> None:
        settings: RateLimitSettings = self.settings

        unknown = [source for source in settings.key_sources if source not in IDENTITY_SOURCES]
        if unknown:
            raise PluginError(
                f"Unknown ratelimit key_sources: {', '.join(unknown)}. "
                f"Valid sources: {', '.join(IDENTITY_SOURCES)}."
            )
        if not settings.key_sources:
            raise PluginError("ratelimit key_sources cannot be empty; nothing would be keyed.")

        try:
            policy = RateLimitPolicy(
                limit=settings.limit, window=settings.window, burst=settings.burst
            )
        except ValueError as exc:
            raise PluginError(f"Invalid ratelimit policy: {exc}") from exc

        # The cache plugin owns the connection. A second pool to the same Redis
        # would double the file descriptors for nothing.
        self._client = ctx.require("cache.client")
        self._limiter = RateLimiter(
            self._client,
            prefix=settings.key_prefix or f"{ctx.settings.app_name}:",
            fail_open=settings.fail_open,
        )
        self._default = RouteLimit(policy, exempt=tuple(settings.exempt_paths))

        ctx.provide("ratelimit", self._limiter)
        ctx.app.state.jfast_ratelimit = _Enforcer(
            self._limiter,
            sources=list(settings.key_sources),
            api_key_header=settings.api_key_header,
        )
        ctx.app.add_exception_handler(RateLimitedError, _rate_limited_handler)

    async def startup(self, ctx: AppContext) -> None:
        if self._default is None:
            return
        installed = install_default_limit(ctx.app, self._default)
        policy = self._default.policy
        ctx.logger.info(
            "rate limit %d/%gs installed on %d route(s)", policy.limit, policy.window, installed
        )

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._limiter is None:
            return HealthReport.fail("rate limiter not initialised")
        if self._limiter.degraded:
            return HealthReport.fail(
                "failing open: rate limit backend unreachable, no limit is being enforced",
                critical=False,
                enforcing=False,
            )
        try:
            await self._client.ping()
        except Exception as exc:  # noqa: BLE001
            return HealthReport.fail(f"redis unreachable: {exc}", critical=False, enforcing=False)
        settings: RateLimitSettings = self.settings
        return HealthReport.ok(
            "enforcing", limit=settings.limit, window=settings.window, enforcing=True
        )
