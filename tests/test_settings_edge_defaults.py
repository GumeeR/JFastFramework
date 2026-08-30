"""The edge protections must be on before anybody configures anything.

``BodySizeLimitMiddleware`` and ``RequestTimeoutMiddleware`` were written,
reviewed and covered by tests -- and shipped switched off, because both
settings defaulted to ``None``. Every generated service therefore had no body
limit and no request timeout at all. These tests are about the default, not
about the middlewares; ``test_edge.py`` already covers those once configured.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Request
from pydantic import ValidationError

from jfastframework.settings import JFastSettings
from jfastframework.testing import build_test_app, client_for

router = APIRouter()


@router.post("/echo")
async def echo(request: Request) -> dict[str, int]:
    return {"size": len(await request.body())}


def _app(**overrides: object):  # type: ignore[no-untyped-def]
    return build_test_app(plugins=[], routers=[router], **overrides)


def _defaults() -> JFastSettings:
    return JFastSettings(_env_file=None)  # type: ignore[call-arg]


# -- the defaults themselves -------------------------------------------


def test_a_body_limit_ships_enabled() -> None:
    assert _defaults().effective_max_body_bytes is not None


def test_a_request_timeout_ships_enabled() -> None:
    assert _defaults().effective_request_timeout is not None


def test_the_default_body_limit_is_generous_enough_for_a_json_api() -> None:
    """Big enough that an ordinary payload never meets it by accident."""
    limit = _defaults().effective_max_body_bytes
    assert limit is not None
    assert limit >= 1024 * 1024


# -- opting out still works --------------------------------------------


def test_zero_disables_the_body_limit() -> None:
    """TOML has no null, so a number has to be able to say "unlimited"."""
    settings = JFastSettings(max_body_bytes=0, _env_file=None)  # type: ignore[call-arg]
    assert settings.effective_max_body_bytes is None


def test_none_disables_the_body_limit() -> None:
    settings = JFastSettings(max_body_bytes=None, _env_file=None)  # type: ignore[call-arg]
    assert settings.effective_max_body_bytes is None


def test_zero_disables_the_request_timeout() -> None:
    settings = JFastSettings(request_timeout=0, _env_file=None)  # type: ignore[call-arg]
    assert settings.effective_request_timeout is None


def test_a_negative_body_limit_is_refused_at_boot() -> None:
    with pytest.raises(ValidationError):
        JFastSettings(max_body_bytes=-1, _env_file=None)  # type: ignore[call-arg]


def test_a_negative_request_timeout_is_refused_at_boot() -> None:
    with pytest.raises(ValidationError):
        JFastSettings(request_timeout=-1.0, _env_file=None)  # type: ignore[call-arg]


# -- and the app actually applies them ---------------------------------


async def test_a_giant_body_is_refused_without_configuring_anything() -> None:
    limit = _defaults().effective_max_body_bytes
    assert limit is not None
    async with client_for(_app()) as client:
        response = await client.post("/echo", content=b"x" * (limit + 1024))
    assert response.status_code == 413
    assert response.json()["title"] == "Payload Too Large"


async def test_an_ordinary_payload_still_passes_on_a_default_install() -> None:
    async with client_for(_app()) as client:
        response = await client.post("/echo", content=b"x" * 100_000)
    assert response.status_code == 200
    assert response.json() == {"size": 100_000}
