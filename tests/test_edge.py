"""Edge protections: body limits, request timeouts, CORS, trusted hosts, docs.

Everything here is off unless configured, so the first assertion in most of
these is that the default behaviour did not change.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import APIRouter, Request
from pydantic import ValidationError

from jfastframework.settings import JFastSettings
from jfastframework.testing import build_test_app, client_for

router = APIRouter()


@router.get("/quick")
async def quick() -> dict[str, str]:
    return {"ok": "yes"}


@router.get("/slow")
async def slow() -> dict[str, str]:
    await asyncio.sleep(5)
    return {"ok": "eventually"}


@router.post("/echo")
async def echo(request: Request) -> dict[str, int]:
    return {"size": len(await request.body())}


def _app(**overrides: object):  # type: ignore[no-untyped-def]
    return build_test_app(plugins=[], routers=[router], **overrides)


# -- defaults ----------------------------------------------------------


async def test_nothing_is_applied_by_default() -> None:
    async with client_for(_app()) as client:
        assert (await client.get("/quick")).status_code == 200
        assert (await client.post("/echo", content=b"x" * 10_000)).json() == {"size": 10_000}


# -- body size ---------------------------------------------------------


async def test_a_declared_body_over_the_limit_is_refused() -> None:
    async with client_for(_app(max_body_bytes=1024)) as client:
        response = await client.post("/echo", content=b"x" * 2048)
    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "Payload Too Large"


async def test_a_body_under_the_limit_passes() -> None:
    async with client_for(_app(max_body_bytes=1024)) as client:
        response = await client.post("/echo", content=b"x" * 100)
    assert response.status_code == 200
    assert response.json() == {"size": 100}


async def test_a_streamed_body_over_the_limit_is_refused() -> None:
    """No Content-Length to check, so the bytes are counted as they arrive."""

    async def chunks():  # type: ignore[no-untyped-def]
        for _ in range(4):
            yield b"x" * 512

    async with client_for(_app(max_body_bytes=1024)) as client:
        response = await client.post("/echo", content=chunks())
    assert response.status_code == 413


# -- request timeout ---------------------------------------------------


async def test_a_request_that_outlives_the_timeout_gets_504() -> None:
    async with client_for(_app(request_timeout=0.05)) as client:
        response = await client.get("/slow")
    assert response.status_code == 504
    assert response.json()["title"] == "Gateway Timeout"


async def test_a_fast_request_is_untouched() -> None:
    async with client_for(_app(request_timeout=1.0)) as client:
        response = await client.get("/quick")
    assert response.status_code == 200


# -- CORS and hosts ----------------------------------------------------


async def test_cors_headers_when_origins_are_configured() -> None:
    app = _app(cors_origins=["https://app.example.com"])
    async with client_for(app) as client:
        response = await client.get("/quick", headers={"Origin": "https://app.example.com"})
    assert response.headers["access-control-allow-origin"] == "https://app.example.com"


async def test_no_cors_headers_when_not_configured() -> None:
    async with client_for(_app()) as client:
        response = await client.get("/quick", headers={"Origin": "https://app.example.com"})
    assert "access-control-allow-origin" not in response.headers


def test_wildcard_origins_with_credentials_is_refused_at_boot() -> None:
    """Browsers reject the pair, so failing here beats failing in a console."""
    with pytest.raises(ValidationError, match="cors_origins cannot be"):
        JFastSettings(cors_origins=["*"], cors_allow_credentials=True, _env_file=None)  # type: ignore[call-arg]


async def test_an_untrusted_host_is_rejected() -> None:
    app = _app(trusted_hosts=["api.example.com"])
    async with client_for(app) as client:
        allowed = await client.get("/quick", headers={"Host": "api.example.com"})
        refused = await client.get("/quick", headers={"Host": "evil.example.com"})
    assert allowed.status_code == 200
    assert refused.status_code == 400


# -- docs in production ------------------------------------------------


def test_docs_are_closed_in_production_by_default() -> None:
    settings = JFastSettings(env="prod", _env_file=None)  # type: ignore[call-arg]
    assert settings.effective_docs_url is None
    assert settings.effective_openapi_url is None


def test_docs_stay_open_when_asked_for_explicitly() -> None:
    settings = JFastSettings(env="prod", docs_url="/internal/docs", _env_file=None)  # type: ignore[call-arg]
    assert settings.effective_docs_url == "/internal/docs"
    # Still closed: each one is its own decision.
    assert settings.effective_openapi_url is None


def test_docs_are_open_outside_production() -> None:
    settings = JFastSettings(env="staging", _env_file=None)  # type: ignore[call-arg]
    assert settings.effective_docs_url == "/docs"


async def test_the_app_serves_no_openapi_in_production() -> None:
    async with client_for(_app(env="prod")) as client:
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/docs")).status_code == 404
        # /info closes in production too; they are now one rule.
        assert (await client.get("/info")).status_code == 404
