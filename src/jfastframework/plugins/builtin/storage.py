"""File storage: named disks, public and private.

    [plugin.storage]
    default = "public"
    serve_local = true          # development only; Caddy or a CDN in production

    [plugin.storage.disks.public]
    driver = "local"
    root = "storage/public"
    visibility = "public"

    [plugin.storage.disks.private]
    driver = "local"
    root = "storage/private"
    visibility = "private"

    [plugin.storage.disks.uploads]
    driver = "s3"
    bucket = "uploads"
    endpoint_url = "http://localhost:9006"   # MinIO
    force_path_style = true

Code writes to a *named disk*; where that disk lives is configuration. The same
handler writes to the filesystem in development and to S3 in production.

Requires: ``pip install jfastframework[storage]`` (add ``[s3]`` for S3/MinIO).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict
from starlette.responses import Response

from jfastframework.errors import ForbiddenError, NotFoundError, PluginError
from jfastframework.plugins.base import (
    HealthReport,
    InfraService,
    Plugin,
    PluginMeta,
    PluginSettings,
)
from jfastframework.storage.base import (
    DiskConfig,
    FileNotFound,
    InvalidKey,
    StorageBackend,
    StorageError,
    sanitised_download_headers,
)
from jfastframework.storage.local import LocalStorage

if TYPE_CHECKING:
    from jfastframework.context import AppContext

logger = logging.getLogger("jfast.storage")

DRIVERS = ("local", "s3")


class StorageSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_STORAGE_", env_file=".env", extra="ignore")

    default: str = "public"
    disks: dict[str, dict[str, Any]] = Field(default_factory=dict)

    # Signs temporary URLs for local disks. S3 disks presign with their own
    # credentials and ignore this.
    signing_key: SecretStr | None = None

    # Serve local disks from this app. Convenient in development; in
    # production Caddy or a CDN should serve the public disk directly, and a
    # Python worker should not be spending its time on static bytes.
    serve_local: bool = True
    prefix: str = "/storage"

    # MinIO in the generated compose file.
    minio_include_infra: bool = False
    minio_port_offset: int = 6


class DiskRegistry:
    """The `storage` provider: `storage.disk("public")`."""

    def __init__(self, disks: dict[str, StorageBackend], default: str) -> None:
        self._disks = disks
        self._default = default

    def disk(self, name: str | None = None) -> StorageBackend:
        chosen = name or self._default
        try:
            return self._disks[chosen]
        except KeyError:
            available = ", ".join(sorted(self._disks)) or "<none>"
            raise PluginError(
                f"No storage disk named {chosen!r}. Available: {available}."
            ) from None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._disks))

    def describe(self) -> dict[str, Any]:
        return {
            "default": self._default,
            "disks": {
                name: {"driver": type(disk).__name__, "visibility": disk.visibility}
                for name, disk in self._disks.items()
            },
        }


DEFAULT_DISKS: dict[str, dict[str, Any]] = {
    "public": {"driver": "local", "root": "storage/public", "visibility": "public"},
    "private": {"driver": "local", "root": "storage/private", "visibility": "private"},
}


class StoragePlugin(Plugin):
    meta = PluginMeta(
        name="storage",
        version="0.1.0",
        description="Named disks over the local filesystem, S3 or MinIO.",
        after=("observability",),
        provides=("storage",),
        default_enabled=False,
        extra="jfastframework[storage]",
    )
    Settings = StorageSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._registry: DiskRegistry | None = None
        self._locals: dict[str, LocalStorage] = {}

    def _build_disk(self, name: str, raw: dict[str, Any]) -> StorageBackend:
        settings: StorageSettings = self.settings
        config = DiskConfig(name=name, **raw)

        if config.driver not in DRIVERS:
            raise PluginError(
                f"storage disk {name!r} has driver {config.driver!r}; "
                f"choose from {', '.join(DRIVERS)}"
            )

        if config.driver == "local":
            disk = LocalStorage(
                name,
                root=config.root,
                visibility=config.visibility,
                url_prefix=config.url_prefix or f"{settings.prefix}/{name}",
                signing_key=(
                    settings.signing_key.get_secret_value() if settings.signing_key else ""
                ),
            )
            self._locals[name] = disk
            return disk

        if not config.bucket:
            raise PluginError(f"storage disk {name!r} uses s3 but has no bucket")

        from jfastframework.storage.s3 import S3Storage

        return S3Storage(
            name,
            bucket=config.bucket,
            visibility=config.visibility,
            region=config.region,
            endpoint_url=config.endpoint_url,
            access_key=config.access_key,
            secret_key=config.secret_key,
            force_path_style=config.force_path_style,
            public_base_url=config.public_base_url,
        )

    def register(self, ctx: AppContext) -> None:
        settings: StorageSettings = self.settings
        raw_disks = settings.disks or DEFAULT_DISKS

        disks = {name: self._build_disk(name, dict(raw)) for name, raw in raw_disks.items()}
        if settings.default not in disks:
            raise PluginError(
                f"storage default is {settings.default!r}, which is not a configured disk. "
                f"Configured: {', '.join(sorted(disks)) or '<none>'}"
            )

        private_locals = [
            name for name, disk in self._locals.items() if disk.visibility != "public"
        ]
        if private_locals and settings.signing_key is None:
            # Without a key, temporary_url() raises at call time -- which is
            # discovered by a user hitting a download link, not by a deploy.
            ctx.logger.warning(
                "storage: no signing key, so temporary URLs are unavailable for %s. "
                "Set JFAST_STORAGE_SIGNING_KEY.",
                ", ".join(private_locals),
            )

        self._registry = DiskRegistry(disks, settings.default)
        ctx.provide("storage", self._registry)

        if settings.serve_local and self._locals:
            ctx.app.include_router(self._build_router(), prefix=settings.prefix, tags=["storage"])
            if ctx.settings.is_production:
                ctx.logger.warning(
                    "storage is serving local disks from the application in production. "
                    "Put Caddy or a CDN in front and set [plugin.storage] serve_local = false."
                )

    def _build_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/{disk}/{key:path}", summary="Download a stored file")
        async def download(disk: str, key: str, request: Request) -> Response:
            backend = self._locals.get(disk)
            if backend is None:
                raise NotFoundError(f"no local disk named {disk!r}")

            if backend.visibility != "public":
                # A private disk is reachable only with a signature covering
                # both the key and the expiry.
                expires = request.query_params.get("expires")
                signature = request.query_params.get("signature")
                if not expires or not signature:
                    raise ForbiddenError("this file requires a signed URL")
                try:
                    valid = backend.verify(key, int(expires), signature)
                except (ValueError, StorageError):
                    valid = False
                if not valid:
                    # One message for "expired" and for "forged": telling them
                    # apart tells an attacker whether the key exists.
                    raise ForbiddenError("this link is invalid or has expired")

            try:
                data = await backend.get(key)
                info = await backend.stat(key)
            except InvalidKey as exc:
                raise NotFoundError(str(exc)) from exc
            except FileNotFound as exc:
                raise NotFoundError(f"{key} not found") from exc

            # attachment + nosniff by default: serving an uploaded .html or
            # .svg inline from this origin runs the uploader's script against
            # your users' cookies.
            headers = sanitised_download_headers(key, info.content_type)
            return Response(content=data, headers=headers)

        return router

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._registry is None:
            return HealthReport.fail("storage not initialised")

        problems: list[str] = []
        for name in self._registry.names:
            healthy, detail = await self._registry.disk(name).health()
            if not healthy:
                problems.append(f"{name}: {detail}")

        meta = self._registry.describe()
        if problems:
            return HealthReport.fail("; ".join(problems), **meta)
        return HealthReport.ok(f"{len(self._registry.names)} disk(s)", **meta)

    def infra(self, ctx: AppContext | None = None) -> list[InfraService]:
        settings: StorageSettings = self.settings
        if not settings.minio_include_infra:
            return []
        return [
            InfraService(
                name="minio",
                image="minio/minio:RELEASE.2024-10-13T13-34-11Z",
                port_offset=settings.minio_port_offset,
                internal_port=9000,
                command="server /data --console-address :9001",
                environment={
                    "MINIO_ROOT_USER": "jfast",
                    "MINIO_ROOT_PASSWORD": "${MINIO_PASSWORD:?set MINIO_PASSWORD}",
                },
                volumes=["minio_data:/data"],
                healthcheck={
                    "test": ["CMD", "mc", "ready", "local"],
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 10,
                },
            )
        ]
