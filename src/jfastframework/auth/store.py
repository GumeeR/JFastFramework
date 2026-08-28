"""Where revoked tokens and refresh families are remembered.

JWTs are stateless, which is the point and also the problem: a token stays
valid until it expires, and "log out" has nothing to act on. The usual answers
are a short access-token lifetime plus a small amount of state for the two
things statelessness cannot do — revocation and refresh-token rotation.

Two implementations, and the difference matters:

* :class:`RedisTokenStore` — shared across processes. What you want in
  production, because a logout on one pod must apply on all of them.
* :class:`MemoryTokenStore` — a dict. Correct for one process, useless behind
  a load balancer, and the health check says so rather than letting you
  discover it from a support ticket.
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TokenStore(Protocol):
    """What the auth plugin needs in order to revoke and to rotate."""

    async def revoke(self, token_id: str, *, ttl: int) -> None:
        """Deny this token id until ``ttl`` seconds from now.

        The TTL is the token's own remaining lifetime: past expiry the
        signature check rejects it anyway, so keeping the entry longer only
        grows the store forever.
        """
        ...

    async def is_revoked(self, token_id: str) -> bool: ...

    async def remember_refresh(self, token_id: str, *, family: str, ttl: int) -> None:
        """Record that this refresh token is the live one for its family."""
        ...

    async def rotate_refresh(self, token_id: str, *, family: str, ttl: int) -> bool:
        """Consume a refresh token. False when it was already used.

        A refresh token presented twice means one of two things: a client
        retried, or a stolen token is being replayed. They are indistinguishable
        from here, so the safe response is the same in both cases -- the caller
        kills the whole family.
        """
        ...

    async def revoke_family(self, family: str, *, ttl: int) -> None: ...

    async def is_family_revoked(self, family: str) -> bool: ...

    async def health(self) -> tuple[bool, str]: ...


class MemoryTokenStore:
    """Single-process store. Fine for development, wrong behind more than one."""

    def __init__(self) -> None:
        self._entries: dict[str, float] = {}

    def _expire(self) -> None:
        now = time.time()
        for key in [k for k, deadline in self._entries.items() if deadline <= now]:
            del self._entries[key]

    async def revoke(self, token_id: str, *, ttl: int) -> None:
        self._entries[f"revoked:{token_id}"] = time.time() + ttl

    async def is_revoked(self, token_id: str) -> bool:
        self._expire()
        return f"revoked:{token_id}" in self._entries

    async def remember_refresh(self, token_id: str, *, family: str, ttl: int) -> None:
        self._entries[f"refresh:{family}:{token_id}"] = time.time() + ttl

    async def rotate_refresh(self, token_id: str, *, family: str, ttl: int) -> bool:
        self._expire()
        key = f"refresh:{family}:{token_id}"
        if key not in self._entries:
            return False
        del self._entries[key]
        return True

    async def revoke_family(self, family: str, *, ttl: int) -> None:
        self._entries[f"family:{family}"] = time.time() + ttl
        for key in [k for k in self._entries if k.startswith(f"refresh:{family}:")]:
            del self._entries[key]

    async def is_family_revoked(self, family: str) -> bool:
        self._expire()
        return f"family:{family}" in self._entries

    async def health(self) -> tuple[bool, str]:
        # Deliberately not "ok": a logout here applies to one process only.
        return (
            False,
            "in-memory token store: revocation does not survive a restart or reach other replicas",
        )


class RedisTokenStore:
    """Shared store. Every entry carries a TTL, so nothing accumulates."""

    def __init__(self, client: Any, *, prefix: str = "jfast:auth:") -> None:
        self._client = client
        self._prefix = prefix

    def _key(self, *parts: str) -> str:
        return self._prefix + ":".join(parts)

    async def revoke(self, token_id: str, *, ttl: int) -> None:
        await self._client.set(self._key("revoked", token_id), "1", ex=max(ttl, 1))

    async def is_revoked(self, token_id: str) -> bool:
        return bool(await self._client.exists(self._key("revoked", token_id)))

    async def remember_refresh(self, token_id: str, *, family: str, ttl: int) -> None:
        await self._client.set(self._key("refresh", family, token_id), "1", ex=max(ttl, 1))

    async def rotate_refresh(self, token_id: str, *, family: str, ttl: int) -> bool:
        # DELETE returns how many keys it removed, so the consume is atomic:
        # two concurrent refreshes cannot both succeed.
        removed = await self._client.delete(self._key("refresh", family, token_id))
        return bool(removed)

    async def revoke_family(self, family: str, *, ttl: int) -> None:
        await self._client.set(self._key("family", family), "1", ex=max(ttl, 1))

    async def is_family_revoked(self, family: str) -> bool:
        return bool(await self._client.exists(self._key("family", family)))

    async def health(self) -> tuple[bool, str]:
        try:
            await self._client.ping()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return False, f"token store unreachable: {exc}"
        return True, "redis token store reachable"
