"""Typed configuration for the JFast kernel.

Rule enforced here: no ``os.getenv`` scattered through service code. Anything
configurable is a validated field, so a bad value fails at boot instead of at
the first request that touches it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "dev", "staging", "prod"]

DEFAULT_CONFIG_FILE = "jfast.toml"


class JFastSettings(BaseSettings):
    """Kernel settings. Plugins own their own settings models."""

    model_config = SettingsConfigDict(
        env_prefix="JFAST_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "jfast-service"
    version: str = "0.1.0"
    env: Environment = "local"
    debug: bool = False

    # A container binds every interface; the container is the network
    # boundary, not this value.
    host: str = "0.0.0.0"  # nosec B104
    port: int = 8000
    root_path: str = ""

    # Plugin selection. `plugins` is the allow-list; when empty, every
    # discovered plugin whose meta says default_enabled is loaded.
    # `disabled_plugins` always wins -- that is how you strip monitoring
    # out of a service without editing code.
    plugins: list[str] = Field(default_factory=list)
    disabled_plugins: list[str] = Field(default_factory=list)

    docs_url: str | None = "/docs"
    openapi_url: str | None = "/openapi.json"

    # Seconds any single plugin gets to answer a readiness probe. A
    # dependency that hangs at the TCP level -- not refused, hung -- would
    # otherwise hold the probe open until the socket gives up, and an
    # orchestrator cannot tell that apart from a slow service.
    readiness_timeout: float = 2.0

    # -- edge protections ------------------------------------------
    #
    # Off unless configured. A body limit or a request timeout is a policy
    # decision with a wrong answer for somebody, so the kernel refuses to
    # guess one. Caddy or an ingress covers these when one is in front --
    # and `jfast deploy function` puts a service on Lambda with nothing in
    # front at all.

    # Exact origins. `["*"]` is accepted and refused in combination with
    # cors_allow_credentials, because browsers reject that pair anyway and
    # failing at boot beats failing in someone's console.
    cors_origins: list[str] = Field(default_factory=list)
    cors_allow_credentials: bool = False
    cors_allow_methods: list[str] = Field(default_factory=lambda: ["*"])
    cors_allow_headers: list[str] = Field(default_factory=lambda: ["*"])

    # Host header allow-list. Empty means every host, which is right
    # behind a proxy that already validated it.
    trusted_hosts: list[str] = Field(default_factory=list)

    # Largest request body accepted, in bytes. None for no limit.
    max_body_bytes: int | None = None

    # Seconds before an unfinished request is answered with 504.
    request_timeout: float | None = None

    @property
    def is_production(self) -> bool:
        return self.env == "prod"

    @property
    def effective_docs_url(self) -> str | None:
        """Interactive docs, closed in production unless asked for.

        ``/info`` already disables itself in production. Leaving ``/docs``
        and the OpenAPI document open was the inconsistency: the schema
        names every route, body field and error, which is a map for anyone
        probing the service. Set ``docs_url`` explicitly to keep it.
        """
        return self._closed_in_production("docs_url", self.docs_url)

    @property
    def effective_openapi_url(self) -> str | None:
        return self._closed_in_production("openapi_url", self.openapi_url)

    def _closed_in_production(self, field: str, value: str | None) -> str | None:
        if self.is_production and field not in self.model_fields_set:
            return None
        return value

    @model_validator(mode="after")
    def _refuse_wildcard_with_credentials(self) -> JFastSettings:
        """A pair browsers reject should fail at boot, not in someone's console."""
        if self.cors_allow_credentials and "*" in self.cors_origins:
            raise ValueError(
                "cors_origins cannot be ['*'] while cors_allow_credentials is on. "
                "Browsers refuse that combination, so it would fail silently at "
                "runtime. List the origins."
            )
        return self


class JFastConfig:
    """Merged view of ``jfast.toml`` plus environment-backed kernel settings.

    ``jfast.toml`` layout::

        [app]
        name = "billing"
        port = 8010

        [plugins]
        enabled = ["observability", "metrics", "database"]
        disabled = ["sentry"]

        [plugin.database]
        pool_size = 20
    """

    def __init__(self, settings: JFastSettings, raw: dict[str, Any] | None = None) -> None:
        self.settings = settings
        self.raw: dict[str, Any] = raw or {}

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = DEFAULT_CONFIG_FILE,
        overrides: dict[str, Any] | None = None,
    ) -> JFastConfig:
        raw: dict[str, Any] = {}
        if config_path is not None:
            path = Path(config_path)
            if path.is_file():
                raw = tomllib.loads(path.read_text(encoding="utf-8"))

        app_section = dict(raw.get("app", {}))
        # `[app] name` reads better in a config file than `app_name`, but the
        # env var has to stay JFAST_APP_NAME to avoid colliding with the very
        # common JFAST_NAME. Map the friendly key onto the field.
        if "name" in app_section:
            app_section.setdefault("app_name", app_section.pop("name"))

        plugins_section = raw.get("plugins", {})
        if "enabled" in plugins_section:
            app_section.setdefault("plugins", plugins_section["enabled"])
        if "disabled" in plugins_section:
            app_section.setdefault("disabled_plugins", plugins_section["disabled"])
        if overrides:
            app_section.update(overrides)

        return cls(settings=JFastSettings(**app_section), raw=raw)

    def plugin_config(self, name: str) -> dict[str, Any]:
        """Raw config block for one plugin (``[plugin.<name>]`` in jfast.toml)."""
        return dict(self.raw.get("plugin", {}).get(name, {}))
