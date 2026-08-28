"""Building blocks for testing JFast services and plugins."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI

from jfastframework.app import create_app
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta
from jfastframework.settings import JFastConfig, JFastSettings


def make_config(
    *,
    app_name: str = "test-service",
    plugins: Sequence[str] = (),
    disabled: Sequence[str] = (),
    raw: dict[str, Any] | None = None,
    **overrides: Any,
) -> JFastConfig:
    """Build a config without touching the filesystem or the environment."""
    overrides.setdefault("env", "local")
    settings = JFastSettings(
        app_name=app_name,
        plugins=list(plugins),
        disabled_plugins=list(disabled),
        _env_file=None,  # type: ignore[call-arg]
        **overrides,
    )
    return JFastConfig(settings=settings, raw=raw or {})


def build_test_app(
    *,
    plugins: Sequence[str] = (),
    disabled: Sequence[str] = (),
    extra_plugins: Sequence[type[Plugin]] = (),
    routers: Sequence[APIRouter] = (),
    raw: dict[str, Any] | None = None,
    **overrides: Any,
) -> FastAPI:
    """Create an app with an explicit plugin list and no config file.

    Explicit beats implicit here: a test that says ``plugins=["observability"]``
    cannot break because someone flipped a default elsewhere.
    """
    config = make_config(plugins=plugins, disabled=disabled, raw=raw, **overrides)
    return create_app(
        config=config,
        plugins=list(extra_plugins),
        routers=list(routers),
    )


@asynccontextmanager
async def client_for(app: FastAPI) -> AsyncIterator[Any]:
    """Async HTTP client bound to an app, with lifespan hooks executed.

    Plugins that open resources in ``startup`` need the lifespan to run;
    ``TestClient`` alone would skip it in async contexts.
    """
    import httpx
    from httpx import ASGITransport

    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        yield client


class NullPlugin(Plugin):
    """Recording plugin for testing the kernel and plugin ordering.

    Subclass it to give it a name::

        class Alpha(NullPlugin):
            meta = PluginMeta(name="alpha", provides=("alpha",))
    """

    meta = PluginMeta(name="null", description="Test double.")

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.events: list[str] = []

    def register(self, ctx: Any) -> None:
        self.events.append("register")
        for key in self.meta.provides:
            ctx.provide(key, self)

    async def startup(self, ctx: Any) -> None:
        self.events.append("startup")

    async def shutdown(self, ctx: Any) -> None:
        self.events.append("shutdown")

    async def health(self, ctx: Any) -> HealthReport:
        return HealthReport.ok("null plugin")
