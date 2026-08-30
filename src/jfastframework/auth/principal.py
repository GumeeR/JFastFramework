"""Who is making this request.

The ``Principal`` is what every handler and every log line sees after the token
has been verified. It is deliberately small: an identity, what it is allowed to
do, and which tenant it belongs to.

Nothing here trusts a header. Before this plugin exists, ``tenant_id`` comes
from ``X-Tenant-ID`` -- convenient in development and forgeable by anyone with
curl. Once auth is on, the tenant comes from a **signed claim**, and the header
is ignored. That upgrade is the main security reason to enable this plugin, not
the login form.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

principal_var: ContextVar[Principal | None] = ContextVar("jfast_principal", default=None)


@dataclass(frozen=True)
class Principal:
    """A verified caller."""

    subject: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)
    tenant_id: str | None = None
    # The token id, for revocation and for correlating a session in the logs.
    token_id: str | None = None
    issuer: str | None = None
    expires_at: datetime | None = None
    # Everything else the token carried. Read it; do not trust anything you
    # did not put there yourself.
    claims: dict[str, Any] = field(default_factory=dict)

    def has_scope(self, *required: str) -> bool:
        """True when every required scope is present."""
        return set(required).issubset(self.scopes)

    def has_any_role(self, *candidates: str) -> bool:
        return bool(self.roles & set(candidates))

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(UTC)

    def describe(self) -> dict[str, Any]:
        """Safe to log: identity and permissions, never the token itself."""
        return {
            "subject": self.subject,
            "tenant_id": self.tenant_id,
            "scopes": sorted(self.scopes),
            "roles": sorted(self.roles),
            "token_id": self.token_id,
        }


@dataclass(frozen=True)
class Grant:
    """What a session is allowed to do, as of now.

    Carried by a refresh token so that rotation can mint an access token with
    the same rights, and returned by an ``on_refresh`` hook when the
    application would rather re-read them from its own user store.
    """

    scopes: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()


def current_principal() -> Principal | None:
    """The caller in this request, or None outside an authenticated one."""
    return principal_var.get()
