"""System endpoints every JFast service exposes.

``/health``  liveness  -- the process is up. Cheap, no dependency probing.
``/ready``   readiness -- every critical plugin reports healthy. This is the
             one your load balancer and orchestrator should poll.
``/info``    build and plugin inventory. Disabled in production by default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from jfastframework.context import AppContext
    from jfastframework.plugins.base import Plugin


def build_system_router(ctx: AppContext, plugins: list[Plugin]) -> APIRouter:
    router = APIRouter(tags=["system"])
    settings = ctx.settings

    @router.get("/health", summary="Liveness probe")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": settings.app_name,
            "version": settings.version,
            "env": settings.env,
        }

    @router.get("/ready", summary="Readiness probe")
    async def ready() -> JSONResponse:
        checks: dict[str, Any] = {}
        degraded = False
        failed = False

        for plugin in plugins:
            try:
                report = await plugin.health(ctx)
            except Exception as exc:  # noqa: BLE001 - a probe must never 500
                checks[plugin.meta.name] = {
                    "healthy": False,
                    "detail": f"health check raised: {exc}",
                    "critical": True,
                }
                failed = True
                continue

            checks[plugin.meta.name] = {
                "healthy": report.healthy,
                "detail": report.detail,
                "critical": report.critical,
                **({"meta": report.meta} if report.meta else {}),
            }
            if not report.healthy:
                if report.critical:
                    failed = True
                else:
                    degraded = True

        status = "unavailable" if failed else ("degraded" if degraded else "ok")
        return JSONResponse(
            status_code=503 if failed else 200,
            content={"status": status, "service": settings.app_name, "checks": checks},
        )

    if not settings.is_production:

        @router.get("/info", summary="Build and plugin inventory")
        async def info() -> dict[str, Any]:
            return {
                "service": settings.app_name,
                "version": settings.version,
                "env": settings.env,
                "providers": list(ctx.providers),
                "plugins": [plugin.describe() for plugin in plugins],
            }

    return router
