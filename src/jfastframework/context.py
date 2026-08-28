"""Shared application context handed to every plugin.

Plugins never import each other. They publish objects into the context under a
string key and consume them the same way. That indirection is what makes the
plugin graph swappable: swap the Redis cache plugin for an in-memory one and
nothing that consumes ``cache`` has to change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, cast

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jfastframework.settings import JFastConfig, JFastSettings

T = TypeVar("T")


class ProviderNotFound(RuntimeError):
    """Raised when a plugin requires a resource nobody provided."""


@dataclass
class AppContext:
    app: FastAPI
    config: JFastConfig
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("jfast"))
    _providers: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def settings(self) -> JFastSettings:
        return self.config.settings

    def provide(self, key: str, value: Any) -> None:
        """Publish a resource for other plugins (``db.engine``, ``cache``)."""
        if key in self._providers:
            raise RuntimeError(
                f"Provider {key!r} already registered. Two plugins claim the same key; "
                f"disable one in jfast.toml."
            )
        self._providers[key] = value

    def require(self, key: str, expected: type[T] | None = None) -> T:
        """Fetch a resource, failing loudly with an actionable message."""
        try:
            value = self._providers[key]
        except KeyError:
            available = ", ".join(sorted(self._providers)) or "<none>"
            raise ProviderNotFound(
                f"No plugin provides {key!r}. Available providers: {available}. "
                f"Enable the plugin that provides it in jfast.toml."
            ) from None
        if expected is not None and not isinstance(value, expected):
            raise TypeError(f"Provider {key!r} is {type(value)!r}, expected {expected!r}")
        return cast("T", value)

    def get(self, key: str, default: Any = None) -> Any:
        return self._providers.get(key, default)

    def has(self, key: str) -> bool:
        return key in self._providers

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def optional(self, key: str, default: Any = None) -> Any:
        """A provider if some plugin published one, otherwise ``default``.

        ``require()`` is for a dependency you cannot work without; this is for
        one that changes what you do. The mail plugin queues when a queue
        exists and sends inline when it does not, and neither is an error.
        """
        return self._providers.get(key, default)
