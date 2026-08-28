"""Sentry error reporting.

Off by default -- a framework should not ship a phone-home that nobody asked
for. Enable it explicitly and set ``JFAST_SENTRY_DSN``.

Requires: ``pip install jfastframework[sentry]``
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings

if TYPE_CHECKING:
    from jfastframework.context import AppContext


class SentrySettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_SENTRY_", env_file=".env", extra="ignore")

    dsn: SecretStr | None = None
    traces_sample_rate: float = 0.1
    profiles_sample_rate: float = 0.0
    send_default_pii: bool = False
    # Adds GET /_debug/sentry to raise a test exception. Never in production.
    debug_endpoint: bool = False


class SentryPlugin(Plugin):
    meta = PluginMeta(
        name="sentry",
        version="0.1.0",
        description="Sentry error and performance reporting.",
        after=("observability",),
        default_enabled=False,
        extra="jfastframework[sentry]",
    )
    Settings = SentrySettings

    def register(self, ctx: AppContext) -> None:
        settings: SentrySettings = self.settings
        if settings.dsn is None:
            ctx.logger.warning("sentry plugin enabled but JFAST_SENTRY_DSN is unset; skipping")
            return

        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=settings.dsn.get_secret_value(),
            environment=ctx.settings.env,
            release=f"{ctx.settings.app_name}@{ctx.settings.version}",
            traces_sample_rate=settings.traces_sample_rate,
            profiles_sample_rate=settings.profiles_sample_rate,
            send_default_pii=settings.send_default_pii,
            integrations=[StarletteIntegration(), FastApiIntegration()],
        )
        sentry_sdk.set_tag("service", ctx.settings.app_name)

        if settings.debug_endpoint and not ctx.settings.is_production:

            @ctx.app.get("/_debug/sentry", include_in_schema=False)
            async def trigger_error() -> None:
                raise RuntimeError("JFast Sentry test exception")

    async def health(self, ctx: AppContext) -> HealthReport:
        if self.settings.dsn is None:
            return HealthReport.fail("sentry DSN not configured", critical=False)
        return HealthReport.ok("sentry initialised")
