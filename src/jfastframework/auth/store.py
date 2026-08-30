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
from typing import Any, Literal, Protocol, runtime_checkable

# What presenting a refresh token turned out to be:
#
# * ``rotated``  -- it was the live one. Mint the next pair.
# * ``raced``    -- it was consumed moments ago, inside the grace window. Two
#                   tabs of one browser, not a thief.
# * ``replayed`` -- it is not live and was not just consumed. Kill the family.
#
# The three cases are one return value rather than a bool plus a follow-up
# question because only the store can answer them together: between a `False`
# and a second round trip asking "was it just rotated?", the winner may not
# have recorded itself yet, and the loser would read its own race as a theft.
RefreshOutcome = Literal["rotated", "raced", "replayed"]


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

    async def rotate_refresh(
        self, token_id: str, *, family: str, ttl: int, grace: int = 0
    ) -> RefreshOutcome:
        """Consume a refresh token and say what presenting it turned out to be.

        A refresh token presented twice means one of three things: two requests
        of the same client raced, a client retried later, or a stolen token is
        being replayed. The last two are indistinguishable from here, so both
        end the family; ``grace`` seconds is how long after a successful
        rotation the first one is still told apart from them.

        ``grace = 0`` disables that: every second presentation is a replay.
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

    async def rotate_refresh(
        self, token_id: str, *, family: str, ttl: int, grace: int = 0
    ) -> RefreshOutcome:
        self._expire()
        key = f"refresh:{family}:{token_id}"
        rotated = f"rotated:{family}:{token_id}"
        if key in self._entries:
            del self._entries[key]
            if grace > 0:
                self._entries[rotated] = time.time() + grace
            return "rotated"
        if rotated in self._entries:
            return "raced"
        return "replayed"

    async def revoke_family(self, family: str, *, ttl: int) -> None:
        self._entries[f"family:{family}"] = time.time() + ttl
        for prefix in (f"refresh:{family}:", f"rotated:{family}:"):
            for key in [k for k in self._entries if k.startswith(prefix)]:
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


# Why a script rather than DELETE followed by a second command: Redis runs a
# script to completion with nothing interleaved, so a request whose DELETE
# returned 0 is guaranteed to see the mark the winner wrote. Two round trips
# have a window between them in which the loser reads no mark and reports a
# theft -- which is the two-tab race, moved rather than fixed.
#
# The mark is written only by the request that won the DELETE, so a replay can
# read it but never create or extend it: the grace window closes on schedule
# however often the token is presented.
ROTATE_REFRESH_LUA = """
local live = KEYS[1]
local mark = KEYS[2]
local grace = tonumber(ARGV[1])

if redis.call('DEL', live) == 1 then
  if grace > 0 then
    redis.call('SET', mark, '1', 'EX', grace)
  end
  return 1
end
if redis.call('EXISTS', mark) == 1 then
  return 2
end
return 0
"""

_OUTCOMES: dict[int, RefreshOutcome] = {0: "replayed", 1: "rotated", 2: "raced"}


class RedisTokenStore:
    """Shared store. Every entry carries a TTL, so nothing accumulates."""

    def __init__(self, client: Any, *, prefix: str = "jfast:auth:") -> None:
        self._client = client
        self._prefix = prefix
        # register_script reloads on NOSCRIPT, so a Redis restarted under us
        # does not turn every refresh into a permanent failure.
        self._rotate_script: Any = client.register_script(ROTATE_REFRESH_LUA)

    def _key(self, *parts: str) -> str:
        return self._prefix + ":".join(parts)

    async def revoke(self, token_id: str, *, ttl: int) -> None:
        await self._client.set(self._key("revoked", token_id), "1", ex=max(ttl, 1))

    async def is_revoked(self, token_id: str) -> bool:
        return bool(await self._client.exists(self._key("revoked", token_id)))

    async def remember_refresh(self, token_id: str, *, family: str, ttl: int) -> None:
        await self._client.set(self._key("refresh", family, token_id), "1", ex=max(ttl, 1))

    async def rotate_refresh(
        self, token_id: str, *, family: str, ttl: int, grace: int = 0
    ) -> RefreshOutcome:
        raw = await self._rotate_script(
            keys=[
                self._key("refresh", family, token_id),
                self._key("rotated", family, token_id),
            ],
            args=[max(grace, 0)],
        )
        return _OUTCOMES[int(raw)]

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
