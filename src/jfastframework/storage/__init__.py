"""File storage: named disks over the local filesystem, S3 or MinIO.

    storage = request.app.state.jfast.require("storage")

    await storage.disk("public").put("avatars/1.png", data, content_type="image/png")
    storage.disk("public").url("avatars/1.png")

    await storage.disk("private").put("invoices/7.pdf", pdf)
    await storage.disk("private").temporary_url("invoices/7.pdf", expires_in=300)

Concrete backends are imported lazily -- S3 carries its own dependency.
"""

from jfastframework.storage.base import (
    DiskConfig,
    FileNotFound,
    InvalidKey,
    StorageBackend,
    StorageError,
    StoredFile,
    UrlSigner,
    guess_content_type,
    normalise_key,
    sanitised_download_headers,
)
from jfastframework.storage.local import LocalStorage
from jfastframework.storage.pipeline import (
    Upload,
    UploadPipeline,
    UploadRejected,
    UploadStep,
    build_pipeline,
    register_step,
    sniff_content_type,
)
from jfastframework.storage.resolve import DiskLedger, InMemoryLedger, KeyResolver

__all__ = [
    "DiskConfig",
    "DiskLedger",
    "FileNotFound",
    "InMemoryLedger",
    "InvalidKey",
    "KeyResolver",
    "LocalStorage",
    "StorageBackend",
    "StorageError",
    "StoredFile",
    "Upload",
    "UploadPipeline",
    "UploadRejected",
    "UploadStep",
    "UrlSigner",
    "build_pipeline",
    "guess_content_type",
    "normalise_key",
    "register_step",
    "sanitised_download_headers",
    "sniff_content_type",
]
