"""Prometheus metrics.

Default-enabled, and the first thing you turn off for a service that should
not carry the dependency::

    [plugins]
    disabled = ["metrics"]

Exposes the RED signals (Rate, Errors, Duration) plus whatever the service
registers on the shared registry, which is published as the ``metrics.registry``
provider.

Requires: ``pip install jfastframework[metrics]``
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from pydantic_settings import SettingsConfigDict
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from jfastframework.plugins.base import (
    HealthReport,
    InfraService,
    Plugin,
    PluginMeta,
    PluginSettings,
)

if TYPE_CHECKING:
    from jfastframework.context import AppContext


class MetricsSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_METRICS_", env_file=".env", extra="ignore")

    path: str = "/metrics"
    # Emit a docker-compose Prometheus + Grafana pair when generating deploys.
    include_infra: bool = False
    prometheus_port_offset: int = 5
    grafana_port_offset: int = 6


class PrometheusMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, requests: Any, latency: Any, in_progress: Any) -> None:
        super().__init__(app)
        self.requests = requests
        self.latency = latency
        self.in_progress = in_progress

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        # Use the route template, not the raw path: /users/42 and /users/43
        # must not create two time series.
        route = request.scope.get("route")
        endpoint = getattr(route, "path", request.url.path)
        method = request.method

        self.in_progress.labels(method=method, endpoint=endpoint).inc()
        started = time.perf_counter()
        status = 500
        try:
            response: Response = await call_next(request)
            status = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - started
            self.in_progress.labels(method=method, endpoint=endpoint).dec()
            self.requests.labels(method=method, endpoint=endpoint, status=str(status)).inc()
            self.latency.labels(method=method, endpoint=endpoint).observe(elapsed)


class MetricsPlugin(Plugin):
    meta = PluginMeta(
        name="metrics",
        version="0.1.0",
        description="Prometheus RED metrics and /metrics endpoint.",
        after=("observability",),
        provides=("metrics.registry",),
        default_enabled=True,
        extra="jfastframework[metrics]",
    )
    Settings = MetricsSettings

    def register(self, ctx: AppContext) -> None:
        from prometheus_client import (
            CollectorRegistry,
            Counter,
            Gauge,
            Histogram,
            generate_latest,
        )
        from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST
        from starlette.responses import PlainTextResponse

        registry = CollectorRegistry()
        labels = {"service": ctx.settings.app_name}

        requests = Counter(
            "http_requests_total",
            "Total HTTP requests",
            ["method", "endpoint", "status"],
            registry=registry,
        )
        latency = Histogram(
            "http_request_duration_seconds",
            "HTTP request latency",
            ["method", "endpoint"],
            registry=registry,
        )
        in_progress = Gauge(
            "http_requests_in_progress",
            "In-flight HTTP requests",
            ["method", "endpoint"],
            registry=registry,
        )
        info = Gauge("service_info", "Service metadata", ["service", "version"], registry=registry)
        info.labels(service=labels["service"], version=ctx.settings.version).set(1)

        ctx.provide("metrics.registry", registry)
        ctx.app.add_middleware(
            PrometheusMiddleware,
            requests=requests,
            latency=latency,
            in_progress=in_progress,
        )

        path = self.settings.path

        @ctx.app.get(path, include_in_schema=False)
        async def metrics_endpoint() -> PlainTextResponse:
            return PlainTextResponse(
                generate_latest(registry).decode("utf-8"),
                media_type=CONTENT_TYPE_LATEST.split(";")[0],
            )

    async def health(self, ctx: AppContext) -> HealthReport:
        return HealthReport.ok("metrics exposed", path=self.settings.path)

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        if not self.settings.include_infra:
            return []
        return [
            InfraService(
                name="prometheus",
                image="prom/prometheus:v2.55.0",
                port_offset=self.settings.prometheus_port_offset,
                internal_port=9090,
                volumes=["./prometheus.yml:/etc/prometheus/prometheus.yml:ro"],
            ),
            InfraService(
                name="grafana",
                image="grafana/grafana:11.3.0",
                port_offset=self.settings.grafana_port_offset,
                internal_port=3000,
                environment={"GF_AUTH_ANONYMOUS_ENABLED": "true"},
                depends_on=["prometheus"],
            ),
        ]
