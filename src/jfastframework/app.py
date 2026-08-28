"""Application factory.

A JFast service's ``main.py`` is this::

    from jfastframework import create_app

    app = create_app()

Everything else -- middleware, observability, database, routers -- arrives
through the plugin graph resolved from ``jfast.toml``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI

from jfastframework.context import AppContext
from jfastframework.errors import install_error_handlers
from jfastframework.health import build_system_router
from jfastframework.plugins import registry
from jfastframework.plugins.base import Plugin
from jfastframework.settings import DEFAULT_CONFIG_FILE, JFastConfig

logger = logging.getLogger("jfast")


def create_app(
    *,
    config: JFastConfig | None = None,
    config_path: str | Path | None = DEFAULT_CONFIG_FILE,
    overrides: dict[str, Any] | None = None,
    plugins: Sequence[type[Plugin]] | None = None,
    routers: Sequence[APIRouter] | None = None,
) -> FastAPI:
    """Build a configured FastAPI application.

    Args:
        config: Pre-built config. Skips loading ``jfast.toml``.
        config_path: Path to ``jfast.toml``. ``None`` to use env vars only.
        overrides: Kernel settings overrides, highest precedence.
        plugins: Extra plugin classes to register without installing them as
            distributions. Useful in tests and for private in-repo plugins.
        routers: Application routers to mount after plugins have registered.
    """
    cfg = config or JFastConfig.load(config_path=config_path, overrides=overrides)
    settings = cfg.settings

    resolved = registry.build(cfg, extra_plugins=list(plugins or []))

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        debug=settings.debug,
        root_path=settings.root_path,
        docs_url=settings.docs_url,
        openapi_url=settings.openapi_url,
        lifespan=_build_lifespan(resolved),
    )

    ctx = AppContext(app=app, config=cfg)
    # Plugins and the lifespan reach the context through app.state; nothing
    # else in the framework uses module-level globals.
    app.state.jfast = ctx
    app.state.plugins = resolved

    install_error_handlers(app, debug=settings.debug)

    for plugin in resolved:
        logger.debug("registering plugin %s", plugin.meta.name)
        plugin.register(ctx)

    app.include_router(build_system_router(ctx, resolved))

    for router in routers or []:
        app.include_router(router)

    logger.info(
        "%s built with plugins: %s",
        settings.app_name,
        ", ".join(p.meta.name for p in resolved) or "<none>",
    )
    return app


def _build_lifespan(plugins: list[Plugin]):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx: AppContext = app.state.jfast
        started: list[Plugin] = []
        try:
            for plugin in plugins:
                await plugin.startup(ctx)
                started.append(plugin)
                logger.debug("started plugin %s", plugin.meta.name)
            yield
        finally:
            # Reverse order, and one plugin failing to shut down must not
            # prevent the rest from releasing their resources.
            for plugin in reversed(started):
                try:
                    await plugin.shutdown(ctx)
                except Exception:
                    logger.exception("plugin %s failed to shut down", plugin.meta.name)

    return lifespan


def get_context(app: FastAPI) -> AppContext:
    """Retrieve the JFast context from a running app."""
    ctx = getattr(app.state, "jfast", None)
    if ctx is None:
        raise RuntimeError("This app was not built by jfastframework.create_app")
    return ctx  # type: ignore[no-any-return]
