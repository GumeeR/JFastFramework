"""Trusted proxies and client-IP resolution.

``X-Forwarded-For`` is written by whoever sent the request. Anything keyed on
the client address -- a rate limit above all -- is decorative until the chain
is only believed when it arrives from an address that really is a proxy. The
framework had ``trusted_hosts`` and nothing for this, so both failure modes
were live at once: behind a balancer every client shared one address, and
without one every client could pick its own.

``test_a_forged_forwarded_for_from_an_untrusted_peer_is_ignored`` is the whole
point of the file.

The section at the bottom runs against a real uvicorn on a real socket. The
ASGI-transport tests above cannot see the bug that shipped: uvicorn rewrites
``scope["client"]`` from ``X-Forwarded-For`` before any application middleware
is entered, so the middleware here was correct and never reached a peer address
that had not already been replaced.
"""

from __future__ import annotations

import os
import socket
import subprocess  # nosec B404
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request
from pydantic import ValidationError

from jfastframework.deploy.compose import render_dockerfile
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


# -- a peer that is not a peer -----------------------------------------


async def test_a_peer_that_appears_in_its_own_chain_gets_no_trust() -> None:
    """An address in the chain it is relaying was not read off a socket.

    Either an outer layer substituted it from this very header, or a direct
    client named itself. The scope looks the same both ways, so neither gets to
    be an identity.
    """
    async with _client(_app(), peer=UNTRUSTED) as client:
        body = (await client.get("/whoami", headers={"X-Forwarded-For": UNTRUSTED})).json()
    assert body["helper"] == "unknown"


async def test_a_substituted_peer_leaves_every_reader_saying_the_same_thing() -> None:
    """One resolver, one answer: no reader gets the forged address instead."""
    chain = f"{CLIENT}, {UNTRUSTED}"
    async with _client(_app(), peer=UNTRUSTED) as client:
        body = (await client.get("/whoami", headers={"X-Forwarded-For": chain})).json()
    assert body["helper"] == "unknown"
    assert body["scope"] == ""
    assert body["state"] == ""


async def test_a_substituted_peer_cannot_carry_a_scheme_either() -> None:
    async with _client(_app(), peer=UNTRUSTED) as client:
        body = (
            await client.get(
                "/whoami",
                headers={"X-Forwarded-For": UNTRUSTED, "X-Forwarded-Proto": "https"},
            )
        ).json()
    assert body["scheme"] == "http"


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


# -- behind a real uvicorn ---------------------------------------------

#: The service the child process serves. Loopback is deliberately left out of
#: ``trusted_proxies``: the test's own peer address has to be an untrusted one
#: for a forged header to be a forgery, and 127.0.0.1 is in the default list.
#: It is also the address uvicorn trusts out of the box, which is what made
#: this bug pass a local check.
SERVICE = """\
from fastapi import APIRouter, Request

from jfastframework.middleware import client_ip
from jfastframework.testing import build_test_app

router = APIRouter()


@router.get("/whoami")
async def whoami(request: Request) -> dict[str, str]:
    return {
        "helper": client_ip(request),
        "scope": request.client.host if request.client else "",
        "scheme": request.url.scheme,
    }


app = build_test_app(
    plugins=[],
    routers=[router],
    trusted_proxies=["172.18.0.0/16"],
    hsts_seconds=31536000,
)
"""

FORGED = "203.0.113.99"


@contextmanager
def _server(tmp_path: Path, *extra: str) -> Iterator[str]:
    """A real uvicorn, on a real socket, serving a real jfast app.

    ``extra`` goes on the command line, so a test can serve the same app the
    way uvicorn ships by default and the way ``jfast serve`` starts it.
    """
    (tmp_path / "main.py").write_text(SERVICE, encoding="utf-8")
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
    except OSError as exc:  # pragma: no cover - depends on the sandbox
        pytest.skip(f"no local socket available: {exc}")

    process = subprocess.Popen(  # nosec B603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            *extra,
        ],
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30.0
        while True:
            if process.poll() is not None:  # pragma: no cover - startup failure
                raise RuntimeError(f"uvicorn exited with {process.returncode}")
            try:
                httpx.get(f"{base}/health", timeout=1.0)
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline:  # pragma: no cover - startup failure
                    raise
                time.sleep(0.1)
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged child
            process.kill()


def test_a_forged_header_cannot_pick_an_identity_behind_a_real_uvicorn(tmp_path: Path) -> None:
    """The reported bypass, run against the server the report ran it against.

    uvicorn is started exactly as it ships -- proxy headers on, loopback
    trusted -- because that is what a service gets when anything other than
    the jfast launcher starts it. Eight forged values, and the service must
    not report eight identities: whatever it resolves, the header does not
    get to choose it.
    """
    with _server(tmp_path) as base:
        seen = {
            httpx.get(
                f"{base}/whoami", headers={"X-Forwarded-For": f"203.0.113.{n}"}, timeout=5.0
            ).json()["helper"]
            for n in range(1, 9)
        }
    assert seen == {"unknown"}, seen


def test_the_launcher_leaves_the_real_peer_in_place(tmp_path: Path) -> None:
    """Started the way jfast starts it, the answer is the peer, not the header."""
    with _server(tmp_path, "--no-proxy-headers") as base:
        body = httpx.get(f"{base}/whoami", headers={"X-Forwarded-For": FORGED}, timeout=5.0).json()
    assert body["helper"] == "127.0.0.1"
    assert body["scope"] == "127.0.0.1"


def test_a_forged_proto_does_not_release_hsts_behind_a_real_uvicorn(tmp_path: Path) -> None:
    """HSTS is gated on ``scope["scheme"]``, which uvicorn also rewrites."""
    with _server(tmp_path) as base:
        response = httpx.get(f"{base}/whoami", headers={"X-Forwarded-Proto": "https"}, timeout=5.0)
    assert "strict-transport-security" not in response.headers
    assert response.json()["scheme"] == "http"


def test_a_forged_proto_does_not_release_hsts_under_the_launcher(tmp_path: Path) -> None:
    with _server(tmp_path, "--no-proxy-headers") as base:
        response = httpx.get(f"{base}/whoami", headers={"X-Forwarded-Proto": "https"}, timeout=5.0)
    assert "strict-transport-security" not in response.headers


# -- the launchers themselves ------------------------------------------


def test_the_generated_entrypoint_turns_uvicorn_s_resolver_off() -> None:
    """The image is the one deployment nobody re-reads before it ships."""
    assert "--no-proxy-headers" in render_dockerfile()


MINIMAL_CONFIG = """\
[service]
name = "probe"
port = 8123
"""


def test_serve_turns_uvicorn_s_resolver_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn
    from typer.testing import CliRunner

    from jfastframework.cli.main import app as cli

    (tmp_path / "jfast.toml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(app_path: str, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    # `serve` chdirs into the service directory and does not chdir back; this
    # makes pytest restore the working directory for the rest of the suite.
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["serve", "--path", str(tmp_path), "--no-reload"])
    assert result.exit_code == 0, result.output
    assert captured["proxy_headers"] is False


def test_dev_turns_uvicorn_s_resolver_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from jfastframework.cli import main as cli_main

    (tmp_path / "jfast.toml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    commands: list[list[str]] = []

    def fake_spawn(command: list[str], **kwargs: Any) -> object:
        commands.append(command)
        return object()

    monkeypatch.setattr(cli_main.devtools, "spawn", fake_spawn)
    monkeypatch.setattr(cli_main.devtools, "supervise", lambda processes: 0)

    result = CliRunner().invoke(
        cli_main.app,
        ["dev", "--path", str(tmp_path), "--no-infra", "--no-migrate", "--no-web"],
    )
    assert result.exit_code == 0, result.output
    assert commands and "--no-proxy-headers" in commands[0]
