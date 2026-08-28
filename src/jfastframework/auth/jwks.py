"""JWKS client: fetch and cache an issuer's public keys.

Asymmetric verification is what makes JWT workable across services. The
identity provider holds the private key; every service fetches the public keys
from its JWKS endpoint and verifies locally. No shared secret, no network hop
per request, and key rotation is a publish rather than a redeploy.

Two failure modes this guards against, both of which are easy to build in by
accident:

**Refresh amplification.** A token carrying an unknown ``kid`` should trigger a
refresh -- that is how rotation is picked up. Refreshing on *every* unknown
``kid`` turns a stream of forged tokens into a denial-of-service against your
identity provider. Refreshes are therefore rate-limited, and a token whose
``kid`` is still unknown afterwards is simply rejected.

**Serving stale keys forever.** If the endpoint is unreachable, the cached keys
keep working -- a JWKS outage must not take every service down with it -- but
the staleness is reported through the health check rather than hidden.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


class JWKSError(RuntimeError):
    """The key set could not be fetched or does not contain the key."""


@dataclass
class JWKSClient:
    url: str
    # How long a fetched key set is considered fresh.
    cache_seconds: int = 3600
    # Floor between refreshes triggered by an unknown kid.
    min_refresh_seconds: int = 60
    timeout: float = 5.0

    _keys: dict[str, Any] = field(default_factory=dict, repr=False)
    _fetched_at: float = 0.0
    _last_attempt: float = 0.0
    _last_error: str | None = None

    async def _fetch(self) -> None:
        import httpx

        self._last_attempt = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            document = response.json()

        keys = {}
        for entry in document.get("keys", []):
            kid = entry.get("kid")
            if kid:
                keys[kid] = entry

        if not keys:
            raise JWKSError(f"{self.url} returned no usable keys")

        self._keys = keys
        self._fetched_at = time.monotonic()
        self._last_error = None

    async def _refresh(self, *, force: bool = False) -> None:
        try:
            await self._fetch()
        except Exception as exc:
            self._last_error = str(exc)
            if force or not self._keys:
                # Nothing cached to fall back on: this request cannot be
                # verified, and saying so beats guessing.
                raise JWKSError(f"cannot fetch JWKS from {self.url}: {exc}") from exc

    async def key_for(self, kid: str | None) -> Any:
        """The signing key for this ``kid``, fetching the set if needed."""
        now = time.monotonic()
        expired = now - self._fetched_at > self.cache_seconds

        if not self._keys or expired:
            await self._refresh(force=not self._keys)

        if kid is None:
            if len(self._keys) == 1:
                return next(iter(self._keys.values()))
            raise JWKSError(
                "the token has no 'kid' and the key set has several keys; "
                "which one signed it is unknowable"
            )

        if kid not in self._keys and (now - self._last_attempt) > self.min_refresh_seconds:
            # Unknown kid: the issuer may have rotated. Refresh at most once
            # per window, so forged kids cannot be used to hammer the issuer.
            await self._refresh()

        try:
            return self._keys[kid]
        except KeyError:
            raise JWKSError(f"no key with kid {kid!r} in {self.url}") from None

    async def health(self) -> tuple[bool, str]:
        if not self._keys:
            return False, self._last_error or "no keys fetched yet"
        age = int(time.monotonic() - self._fetched_at)
        if self._last_error:
            # Serving cached keys through an outage is correct; hiding that it
            # is happening is not.
            return False, f"serving keys cached {age}s ago; last refresh failed: {self._last_error}"
        return True, f"{len(self._keys)} key(s), cached {age}s ago"

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))
