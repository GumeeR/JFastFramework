"""Typed configuration for the JFast kernel.

Rule enforced here: no ``os.getenv`` scattered through service code. Anything
configurable is a validated field, so a bad value fails at boot instead of at
the first request that touches it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
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

    host: str = "0.0.0.0"
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

    @property
    def is_production(self) -> bool:
        return self.env == "prod"


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
