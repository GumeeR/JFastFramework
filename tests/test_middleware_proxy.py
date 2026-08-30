"""Trusted proxies and client-IP resolution.

``X-Forwarded-For`` is written by whoever sent the request. Anything keyed on
the client address -- a rate limit above all -- is decorative until the chain
is only believed when it arrives from an address that really is a proxy. The
framework had ``trusted_hosts`` and nothing for this, so both failure modes
were live at once: behind a balancer every client shared one address, and
without one every client could pick its own.

``test_a_forged_forwarded_for_from_an_untrusted_peer_is_ignored`` is the whole
point of the file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request
from pydantic import ValidationError

from jfastframework.middleware import TrustedProxies, client_ip
from jfastframework.settings import JFastSettings
from jfastframework.testing import build_test_app

# TEST-NET-3: an address on the public internet, which is what a client
# talking straight to the service looks like.
UNTRUSTED = "203.0.113.9"
# Where Caddy lands on a compose bridge network, and inside the default
# trusted range for exactly that reason.
PROXY = "172.18.0.5"
CLIENT = "198.51.100.7"

router = APIRouter()


@router.get("/whoami")
async def whoami(request: Request) -> dict[str, str]:
    return {
        "helper": client_ip(request),
        "scope": request.client.host if request.client else "",
        "state": getattr(request.state, "client_ip", ""),
        "scheme": request.url.scheme,
    }


@asynccontextmanager
async def _client(app: FastAPI, *, peer: str) -> AsyncIterator[httpx.AsyncClient]:
    """A client whose requests arrive from ``peer``, the way a socket would."""
    transport = httpx.ASGITransport(app=app, client=(peer, 51234))
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        yield client


def _app(**overrides: Any) -> FastAPI:
    return build_test_app(plugins=[], routers=[router], **overrides)


# -- the bypass --------------------------------------------------------


async def test_a_forged_forwarded_for_from_an_untrusted_peer_is_ignored() -> None:
    """The request that would otherwise choose its own rate-limit bucket."""
    async with _client(_app(), peer=UNTRUSTED) as client:
        body = (await client.get("/whoami", headers={"X-Forwarded-For": "1.2.3.4"})).json()
    assert body["helper"] == UNTRUSTED
    assert body["scope"] == UNTRUSTED


async def test_a_forged_chain_cannot_hide_the_real_address_either() -> None:
    """Padding the chain with trusted-looking hops changes nothing."""
    forged = f"1.2.3.4, {PROXY}, 127.0.0.1"
    async with _client(_app(), peer=UNTRUSTED) as client:
        body = (await client.get("/whoami", headers={"X-Forwarded-For": forged})).json()
    assert body["helper"] == UNTRUSTED


async def test_a_forged_proto_from_an_untrusted_peer_is_ignored() -> None:
    async with _client(_app(), peer=UNTRUSTED) as client:
        body = (await client.get("/whoami", headers={"X-Forwarded-Proto": "https"})).json()
    assert body["scheme"] == "http"


# -- and the case it exists to serve -----------------------------------


async def test_a_trusted_proxy_is_believed() -> None:
    async with _client(_app(), peer=PROXY) as client:
        body = (await client.get("/whoami", headers={"X-Forwarded-For": CLIENT})).json()
    assert body["helper"] == CLIENT
    assert body["scope"] == CLIENT


async def test_the_chain_is_walked_from_the_right() -> None:
    """The rightmost hop that is not a proxy of ours is the real client.

    Everything left of it was written by somebody we have no reason to
    believe, including the client itself.
    """
    chain = f"1.2.3.4, {CLIENT}, 172.18.0.9"
    async with _client(_app(), peer=PROXY) as client:
        body = (await client.get("/whoami", headers={"X-Forwarded-For": chain})).json()
    assert body["helper"] == CLIENT


async def test_a_trusted_proto_is_believed() -> None:
    async with _client(_app(), peer=PROXY) as client:
        body = (await client.get("/whoami", headers={"X-Forwarded-Proto": "https"})).json()
    assert body["scheme"] == "https"


async def test_the_helper_and_request_client_agree() -> None:
    """Application code reading ``request.client.host`` gets the same answer."""
    async with _client(_app(), peer=PROXY) as client:
        body = (await client.get("/whoami", headers={"X-Forwarded-For": CLIENT})).json()
    assert body["helper"] == body["scope"] == body["state"] == CLIENT


async def test_an_empty_trusted_list_believes_nobody() -> None:
    async with _client(_app(trusted_proxies=[]), peer=PROXY) as client:
        body = (await client.get("/whoami", headers={"X-Forwarded-For": CLIENT})).json()
    assert body["helper"] == PROXY


async def test_a_malformed_chain_falls_back_to_the_peer() -> None:
    """RFC 7239 lets a proxy write ``unknown``; that is not an address."""
    async with _client(_app(), peer=PROXY) as client:
        body = (await client.get("/whoami", headers={"X-Forwarded-For": "unknown"})).json()
    assert body["helper"] == PROXY


# -- the resolver on its own -------------------------------------------


def test_cidr_membership() -> None:
    trusted = TrustedProxies(["10.0.0.0/8", "::1/128"])
    assert trusted.is_trusted("10.4.5.6")
    assert trusted.is_trusted("::1")
    assert not trusted.is_trusted("11.0.0.1")
    assert not trusted.is_trusted(None)


def test_a_port_suffix_does_not_defeat_the_check() -> None:
    trusted = TrustedProxies(["10.0.0.0/8"])
    assert trusted.is_trusted("10.4.5.6:41234")
    assert trusted.is_trusted("[::ffff:10.4.5.6]:41234")


def test_a_star_trusts_everything() -> None:
    trusted = TrustedProxies(["*"])
    assert trusted.is_trusted(UNTRUSTED)
    assert trusted.resolve(UNTRUSTED, "1.2.3.4") == "1.2.3.4"


def test_the_default_list_covers_a_compose_bridge_and_a_pod_network() -> None:
    trusted = TrustedProxies(JFastSettings(_env_file=None).trusted_proxies)  # type: ignore[call-arg]
    assert trusted.is_trusted("172.18.0.5")
    assert trusted.is_trusted("10.42.0.11")
    assert trusted.is_trusted("127.0.0.1")
    assert not trusted.is_trusted(UNTRUSTED)


def test_a_bad_cidr_fails_at_boot() -> None:
    with pytest.raises(ValidationError):
        JFastSettings(trusted_proxies=["not-an-address"], _env_file=None)  # type: ignore[call-arg]
