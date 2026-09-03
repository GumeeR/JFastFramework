"""Storage: keys that must be rejected, and links that must expire.

Most of this file is about the two ways a storage layer leaks: a key that
escapes its disk, and a private file reachable without a valid signature.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest

from jfastframework.errors import PluginError
from jfastframework.plugins.builtin.storage import DEFAULT_DISKS, StoragePlugin
from jfastframework.storage.base import (
    InvalidKey,
    StorageBackend,
    normalise_key,
    sanitised_download_headers,
)
from jfastframework.storage.local import FileNotFound, LocalStorage, StorageError
from jfastframework.storage.pipeline import (
    STEP_FACTORIES,
    Upload,
    UploadRejected,
    parse_size,
    register_step,
    sniff_content_type,
)
from jfastframework.testing import build_test_app, client_for


def disk(tmp_path: Path, **overrides: object) -> LocalStorage:
    params: dict[str, object] = {
        "root": tmp_path / "files",
        "visibility": "private",
        "url_prefix": "/storage/private",
        "signing_key": "test-signing-key-at-least-32-bytes-long",
    }
    params.update(overrides)
    return LocalStorage("private", **params)  # type: ignore[arg-type]


# -- keys ---------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "../etc/passwd",
        "a/../../b",
        "/etc/passwd",
        "windows\\path",
        "nul\x00byte",
        "",
        ".",
        "..",
        "a/../..",
        "café.png",  # non-ASCII: portable across backends is the point
        "sp ace.txt",
    ],
)
def test_unsafe_keys_are_rejected(key: str) -> None:
    with pytest.raises(InvalidKey):
        normalise_key(key)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("invoices/2026/inv-1.pdf", "invoices/2026/inv-1.pdf"),
        ("a/./b.txt", "a/b.txt"),
        ("deep/a/b/../c.txt", "deep/a/c.txt"),
        ("a//b.txt", "a/b.txt"),
    ],
)
def test_safe_keys_are_canonicalised(key: str, expected: str) -> None:
    assert normalise_key(key) == expected


async def test_traversal_cannot_write_outside_the_root(tmp_path: Path) -> None:
    store = disk(tmp_path)
    with pytest.raises(InvalidKey):
        await store.put("../escaped.txt", b"x")
    assert not (tmp_path / "escaped.txt").exists()


async def test_symlink_out_of_the_root_is_refused(tmp_path: Path) -> None:
    store = disk(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    # normalise_key passes here: the key itself is innocent. Only resolving
    # the symlink reveals that it leaves the disk.
    try:
        (tmp_path / "files" / "link").symlink_to(outside)
    except OSError as exc:  # pragma: no cover - Windows without the privilege
        # Creating a symlink on Windows needs SeCreateSymbolicLinkPrivilege,
        # which a normal account does not have unless Developer Mode is on.
        # Skipping is honest; failing would report the guard as broken on a
        # machine where the test could not build its own precondition.
        pytest.skip(f"cannot create a symlink here: {exc}")
    with pytest.raises(StorageError):
        await store.put("link/secret.txt", b"x")


# -- round trip ---------------------------------------------------------


async def test_put_get_stat_delete(tmp_path: Path) -> None:
    store = disk(tmp_path)
    stored = await store.put("docs/report.pdf", b"hello")

    assert stored.size == 5
    assert stored.content_type == "application/pdf"
    assert await store.get("docs/report.pdf") == b"hello"
    assert await store.exists("docs/report.pdf")
    assert (await store.stat("docs/report.pdf")).size == 5

    assert await store.delete("docs/report.pdf") is True
    # Deleting again is not an error, but it is also not a success.
    assert await store.delete("docs/report.pdf") is False
    with pytest.raises(FileNotFound):
        await store.get("docs/report.pdf")


async def test_put_replaces_without_a_partial_window(tmp_path: Path) -> None:
    store = disk(tmp_path)
    await store.put("a.txt", b"first")
    await store.put("a.txt", b"second-and-longer")
    assert await store.get("a.txt") == b"second-and-longer"
    # The temp file used for the atomic replace must not be left behind.
    leftovers = [p.name for p in (tmp_path / "files").iterdir() if p.name != "a.txt"]
    assert leftovers == []


async def test_listing_is_prefixed_and_limited(tmp_path: Path) -> None:
    store = disk(tmp_path)
    for i in range(5):
        await store.put(f"inv/{i}.txt", b"x")
    await store.put("other/1.txt", b"x")

    keys = sorted(f.key for f in await store.listing("inv/"))
    assert keys == [f"inv/{i}.txt" for i in range(5)]
    assert len(await store.listing("inv/", limit=2)) == 2


# -- URLs ---------------------------------------------------------------


def test_private_disk_refuses_a_permanent_url(tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="private"):
        disk(tmp_path).url("secret.pdf")


def test_public_disk_gives_a_permanent_url(tmp_path: Path) -> None:
    store = disk(tmp_path, visibility="public", url_prefix="/storage/public")
    assert store.url("logo.png") == "/storage/public/logo.png"


async def test_signature_covers_the_key(tmp_path: Path) -> None:
    store = disk(tmp_path)
    expires = int(time.time()) + 300
    signature = store.sign("invoices/mine.pdf", expires)
    # The signature is over key *and* expiry, so it does not carry to another
    # file. Without that, one valid link is a key to the whole disk.
    assert store.verify("invoices/mine.pdf", expires, signature) is True
    assert store.verify("invoices/yours.pdf", expires, signature) is False


async def test_signature_covers_the_expiry(tmp_path: Path) -> None:
    store = disk(tmp_path)
    expires = int(time.time()) + 300
    signature = store.sign("a.pdf", expires)
    assert store.verify("a.pdf", expires + 86400, signature) is False


async def test_expired_signature_is_refused(tmp_path: Path) -> None:
    store = disk(tmp_path)
    expired = int(time.time()) - 1
    assert store.verify("a.pdf", expired, store.sign("a.pdf", expired)) is False


def test_signing_without_a_key_fails_loudly(tmp_path: Path) -> None:
    store = disk(tmp_path, signing_key="")
    with pytest.raises(StorageError, match="signing key"):
        store.sign("a.pdf", 0)


# -- download headers ---------------------------------------------------


def test_uploads_are_served_as_attachments() -> None:
    headers = sanitised_download_headers("evil.html", "text/html")
    # Rendering this inline would run the uploader's script on our origin.
    assert headers["Content-Disposition"].startswith("attachment")
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_filename_cannot_inject_a_header() -> None:
    headers = sanitised_download_headers("a\r\nX-Evil: 1.txt", "text/plain")
    assert "\r" not in headers["Content-Disposition"]
    assert "\n" not in headers["Content-Disposition"]


# -- the plugin ---------------------------------------------------------


def storage_app(tmp_path: Path, **storage_config: object) -> object:
    config: dict[str, object] = {
        "default": "public",
        "signing_key": "test-signing-key-at-least-32-bytes-long",
        "disks": {
            "public": {
                "driver": "local",
                "root": str(tmp_path / "public"),
                "visibility": "public",
            },
            "private": {
                "driver": "local",
                "root": str(tmp_path / "private"),
                "visibility": "private",
            },
        },
    }
    config.update(storage_config)
    return build_test_app(
        plugins=["observability", "storage"],
        extra_plugins=[StoragePlugin],
        raw={"plugin": {"storage": config}},
    )


def test_default_disks_are_public_and_private() -> None:
    assert set(DEFAULT_DISKS) == {"public", "private"}
    assert DEFAULT_DISKS["public"]["visibility"] == "public"
    assert DEFAULT_DISKS["private"]["visibility"] == "private"


def test_unknown_default_disk_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="not a configured disk"):
        storage_app(tmp_path, default="nowhere")


def test_unknown_driver_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="driver"):
        build_test_app(
            plugins=["observability", "storage"],
            extra_plugins=[StoragePlugin],
            raw={
                "plugin": {
                    "storage": {
                        "default": "d",
                        "disks": {"d": {"driver": "ftp", "root": str(tmp_path)}},
                    }
                }
            },
        )


async def test_public_file_is_served_without_a_signature(tmp_path: Path) -> None:
    app = storage_app(tmp_path)
    async with client_for(app) as client:  # type: ignore[arg-type]
        registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
        await registry.disk("public").put("logo.txt", b"pixels")

        response = await client.get("/storage/public/logo.txt")
        assert response.status_code == 200
        assert response.content == b"pixels"
        assert response.headers["x-content-type-options"] == "nosniff"


async def test_private_file_needs_a_signature(tmp_path: Path) -> None:
    app = storage_app(tmp_path)
    async with client_for(app) as client:  # type: ignore[arg-type]
        registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
        private = registry.disk("private")
        await private.put("invoice.txt", b"secret")

        assert (await client.get("/storage/private/invoice.txt")).status_code == 403

        # Build the URL from the backend's own output rather than by hand: a
        # hand-written query string tests the test, not the plugin.
        signed = await private.temporary_url("invoice.txt", expires_in=60)
        response = await client.get(signed)
        assert response.status_code == 200
        assert response.content == b"secret"


async def test_forged_signature_is_refused(tmp_path: Path) -> None:
    app = storage_app(tmp_path)
    async with client_for(app) as client:  # type: ignore[arg-type]
        registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
        await registry.disk("private").put("invoice.txt", b"secret")

        expires = int(time.time()) + 300
        response = await client.get(
            f"/storage/private/invoice.txt?expires={expires}&signature=not-a-signature"
        )
        assert response.status_code == 403


async def test_expired_link_and_forged_link_look_the_same(tmp_path: Path) -> None:
    """Different messages would tell an attacker whether the key exists."""
    app = storage_app(tmp_path)
    async with client_for(app) as client:  # type: ignore[arg-type]
        registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
        private = registry.disk("private")
        await private.put("invoice.txt", b"secret")

        past = int(time.time()) - 10
        stale = private.sign("invoice.txt", past)
        expired = await client.get(f"/storage/private/invoice.txt?expires={past}&signature={stale}")
        forged = await client.get(
            f"/storage/private/invoice.txt?expires={int(time.time()) + 60}&signature=xxx"
        )
        assert expired.status_code == forged.status_code == 403
        assert expired.json()["detail"] == forged.json()["detail"]


async def test_traversal_through_the_route_is_a_404(tmp_path: Path) -> None:
    app = storage_app(tmp_path)
    async with client_for(app) as client:  # type: ignore[arg-type]
        response = await client.get("/storage/public/..%2f..%2fetc%2fpasswd")
        assert response.status_code == 404


def test_disk_registry_names_the_alternatives(tmp_path: Path) -> None:
    app = storage_app(tmp_path)
    registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
    with pytest.raises(PluginError, match="private, public"):
        registry.disk("nope")


# -- public_base_url ----------------------------------------------------


def test_local_disk_honours_public_base_url(tmp_path: Path) -> None:
    store = disk(
        tmp_path,
        visibility="public",
        url_prefix="/storage/public",
        public_base_url="https://cdn.example.com/assets",
    )
    assert store.url("logo.png") == "https://cdn.example.com/assets/logo.png"


def test_public_base_url_reaches_a_local_disk_through_the_plugin(tmp_path: Path) -> None:
    app = storage_app(
        tmp_path,
        disks={
            "public": {
                "driver": "local",
                "root": str(tmp_path / "public"),
                "visibility": "public",
                "public_base_url": "https://cdn.example.com",
            }
        },
    )
    registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
    assert registry.disk("public").url("logo.png") == "https://cdn.example.com/logo.png"


async def test_a_signed_url_is_not_sent_to_the_cdn(tmp_path: Path) -> None:
    """A cache in front of a signed URL outlives the signature."""
    store = disk(tmp_path, public_base_url="https://cdn.example.com")
    signed = await store.temporary_url("invoice.pdf")
    assert signed.startswith("/storage/private/invoice.pdf?")


def test_a_key_the_driver_ignores_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="s3 driver"):
        storage_app(
            tmp_path,
            disks={
                "public": {
                    "driver": "local",
                    "root": str(tmp_path / "public"),
                    "bucket": "nope",
                }
            },
        )


def test_an_unknown_disk_key_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="'visibilty'"):
        storage_app(
            tmp_path,
            disks={
                "public": {
                    "driver": "local",
                    "root": str(tmp_path / "public"),
                    "visibilty": "public",
                }
            },
        )


# -- the upload pipeline ------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PDF = b"%PDF-1.7\n" + b"\x00" * 16
ZIP = b"PK\x03\x04" + b"\x00" * 16


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, "image/png"),
        (PDF, "application/pdf"),
        (ZIP, "application/zip"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 12, "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"\x00\x00\x00\x20ftypavif" + b"\x00" * 8, "image/avif"),
        # An extension is not evidence: this is a shell script called .png.
        (b"#!/bin/sh\nrm -rf /\n", None),
        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", None),
        (b"", None),
    ],
)
def test_content_type_comes_from_the_bytes(data: bytes, expected: str | None) -> None:
    assert sniff_content_type(data) == expected


@pytest.mark.parametrize(
    ("written", "expected"),
    [("10MB", 10 * 1024**2), ("512KB", 512 * 1024), ("1.5GB", 1610612736), ("900", 900)],
)
def test_sizes_are_read_in_binary_units(written: str, expected: int) -> None:
    assert parse_size(written) == expected


@pytest.mark.parametrize("written", ["ten megabytes", "-4MB", "0", "10TB", ""])
def test_an_unreadable_size_is_refused(written: str) -> None:
    with pytest.raises(ValueError, match=r"size|read"):
        parse_size(written)


def pipeline_app(tmp_path: Path, **validate: object) -> object:
    return storage_app(
        tmp_path,
        default="uploads",
        disks={
            "uploads": {
                "driver": "local",
                "root": str(tmp_path / "uploads"),
                "visibility": "private",
                "pipeline": ["validate"],
                "validate": {"max_bytes": "1KB", "allow": ["image/png"], **validate},
            }
        },
    )


def uploads(app: object) -> StorageBackend:
    registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
    return registry.disk("uploads")  # type: ignore[no-any-return]


async def test_validate_rejects_an_upload_over_the_limit(tmp_path: Path) -> None:
    with pytest.raises(UploadRejected, match=r"4\.0KB \(4120 bytes\).*max_bytes of 1KB"):
        await uploads(pipeline_app(tmp_path)).put("big.png", PNG + b"\x00" * 4096)


async def test_validate_reads_the_type_from_the_bytes_not_the_name(tmp_path: Path) -> None:
    # A PDF called .png, declared image/png by the client. Only the bytes are
    # evidence, and they say this disk does not take it.
    with pytest.raises(UploadRejected, match="application/pdf"):
        await uploads(pipeline_app(tmp_path)).put("avatar.png", PDF, content_type="image/png")


async def test_validate_refuses_what_it_cannot_identify(tmp_path: Path) -> None:
    with pytest.raises(UploadRejected, match="cannot identify"):
        await uploads(pipeline_app(tmp_path)).put("notes.png", b"just some text, honestly")


async def test_validate_refuses_an_empty_upload(tmp_path: Path) -> None:
    with pytest.raises(UploadRejected, match="empty"):
        await uploads(pipeline_app(tmp_path)).put("nothing.png", b"")


async def test_the_stored_type_is_the_sniffed_one(tmp_path: Path) -> None:
    app = pipeline_app(tmp_path)
    # The client says text/html, which is what would come back on download.
    stored = await uploads(app).put("avatar.png", PNG, content_type="text/html")
    assert stored.content_type == "image/png"


async def test_a_disk_without_a_pipeline_is_unchanged(tmp_path: Path) -> None:
    app = storage_app(tmp_path)
    registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
    stored = await registry.disk("public").put("notes.txt", b"anything at all")
    assert stored.size == 15


async def test_a_registered_step_can_rewrite_the_bytes(tmp_path: Path) -> None:
    """The pipeline is the extension point image work will plug into."""

    class Stamp:
        name = "stamp"

        def __init__(self, disk: str, config: dict[str, object]) -> None:
            self.suffix = str(config.get("suffix", ""))

        async def process(self, upload: Upload) -> Upload:
            return replace(upload, data=upload.data + self.suffix.encode())

    register_step("stamp", Stamp)
    try:
        app = storage_app(
            tmp_path,
            default="marked",
            disks={
                "marked": {
                    "driver": "local",
                    "root": str(tmp_path / "marked"),
                    "pipeline": ["validate", "stamp"],
                    "validate": {"allow": ["image/png"]},
                    "stamp": {"suffix": "-seen"},
                }
            },
        )
        registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
        await registry.disk("marked").put("a.png", PNG)
        assert (await registry.disk("marked").get("a.png")).endswith(b"-seen")
    finally:
        STEP_FACTORIES.pop("stamp", None)


def test_an_unknown_step_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="no pipeline step named 'antivirus'"):
        storage_app(
            tmp_path,
            default="d",
            disks={"d": {"driver": "local", "root": str(tmp_path), "pipeline": ["antivirus"]}},
        )


def test_a_step_configured_but_not_run_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="does not run it"):
        storage_app(
            tmp_path,
            default="d",
            disks={
                "d": {
                    "driver": "local",
                    "root": str(tmp_path),
                    "validate": {"max_bytes": "1MB"},
                }
            },
        )


def test_validate_refuses_a_type_it_could_never_recognise(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="cannot recognise"):
        storage_app(
            tmp_path,
            default="d",
            disks={
                "d": {
                    "driver": "local",
                    "root": str(tmp_path),
                    "pipeline": ["validate"],
                    "validate": {"allow": ["image/svg+xml"]},
                }
            },
        )


# -- resolution without a disk name -------------------------------------


def resolving_app(tmp_path: Path, **overrides: object) -> object:
    return storage_app(tmp_path, resolve_by_key=True, **overrides)


async def test_a_file_survives_being_moved_between_disks(tmp_path: Path) -> None:
    app = resolving_app(tmp_path)
    async with client_for(app) as client:  # type: ignore[arg-type]
        registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
        await registry.disk("public").put("logo.txt", b"pixels")
        await registry.record("logo.txt", "public")

        stable = await client.get("/storage/logo.txt")
        assert stable.status_code == 200
        assert stable.content == b"pixels"

        # The URL above names no disk, so moving the object must not break it.
        await registry.move("logo.txt", "public", "private")
        assert await registry.disk("public").exists("logo.txt") is False
        assert (await client.get("/storage/logo.txt")).status_code == 403

        signed = await registry.stable_temporary_url("logo.txt", expires_in=60)
        moved = await client.get(signed)
        assert moved.status_code == 200
        assert moved.content == b"pixels"


async def test_the_old_disk_named_urls_still_work(tmp_path: Path) -> None:
    app = resolving_app(tmp_path)
    async with client_for(app) as client:  # type: ignore[arg-type]
        registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
        await registry.disk("public").put("logo.txt", b"pixels")
        response = await client.get("/storage/public/logo.txt")
        assert response.status_code == 200
        assert response.content == b"pixels"


async def test_a_nested_key_resolves_when_its_first_segment_is_not_a_disk(
    tmp_path: Path,
) -> None:
    app = resolving_app(tmp_path)
    async with client_for(app) as client:  # type: ignore[arg-type]
        registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
        await registry.disk("public").put("invoices/2026/1042.txt", b"total")
        await registry.record("invoices/2026/1042.txt", "public")

        response = await client.get("/storage/invoices/2026/1042.txt")
        assert response.status_code == 200
        assert response.content == b"total"


async def test_an_unrecorded_key_is_a_404(tmp_path: Path) -> None:
    app = resolving_app(tmp_path)
    async with client_for(app) as client:  # type: ignore[arg-type]
        registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
        await registry.disk("public").put("logo.txt", b"pixels")
        # Recorded resolution is exact, so an object nobody wrote down is not
        # found even though it is sitting on a configured disk.
        assert (await client.get("/storage/logo.txt")).status_code == 404


async def test_probing_finds_the_object_on_the_older_disk(tmp_path: Path) -> None:
    app = resolving_app(tmp_path, resolve_strategy="probe", read_order=["private", "public"])
    async with client_for(app) as client:  # type: ignore[arg-type]
        registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
        await registry.disk("public").put("logo.txt", b"pixels")

        found = await client.get("/storage/logo.txt")
        assert found.status_code == 200
        assert found.content == b"pixels"
        # Probing alone does not migrate anything.
        assert await registry.disk("private").exists("logo.txt") is False


async def test_copy_on_read_closes_the_migration_window(tmp_path: Path) -> None:
    app = resolving_app(
        tmp_path,
        resolve_strategy="probe",
        read_order=["private", "public"],
        copy_on_read=True,
    )
    registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
    await registry.disk("public").put("logo.txt", b"pixels")

    name, _ = await registry.resolve("logo.txt")
    assert name == "private"
    assert await registry.disk("private").get("logo.txt") == b"pixels"
    # A copy, not a move: nothing is deleted while other links may still name
    # the old disk directly.
    assert await registry.disk("public").exists("logo.txt") is True


def test_probing_without_a_read_order_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="read_order is empty"):
        resolving_app(tmp_path, resolve_strategy="probe")


def test_read_order_naming_a_missing_disk_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="not configured disks"):
        resolving_app(tmp_path, resolve_strategy="probe", read_order=["glacier"])


def test_copy_on_read_without_probing_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="copy_on_read"):
        resolving_app(tmp_path, copy_on_read=True)


def test_a_read_order_that_nothing_reads_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="only read while probing"):
        resolving_app(tmp_path, read_order=["public"])


def test_resolution_settings_without_resolution_are_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="resolve_by_key is false"):
        storage_app(tmp_path, read_order=["public"])


async def test_moving_does_not_re_run_the_target_pipeline(tmp_path: Path) -> None:
    """A rule tightened today must not fail the migration of older files."""
    app = storage_app(
        tmp_path,
        default="old",
        disks={
            "old": {"driver": "local", "root": str(tmp_path / "old")},
            "new": {
                "driver": "local",
                "root": str(tmp_path / "new"),
                "pipeline": ["validate"],
                "validate": {"max_bytes": "1KB", "allow": ["image/png"]},
            },
        },
    )
    registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
    await registry.disk("old").put("notes.txt", b"plain text, written before the rule")

    await registry.move("notes.txt", "old", "new")
    assert await registry.disk("new").get("notes.txt") == b"plain text, written before the rule"


async def test_resolution_is_off_unless_asked_for(tmp_path: Path) -> None:
    app = storage_app(tmp_path)
    async with client_for(app) as client:  # type: ignore[arg-type]
        registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
        await registry.disk("public").put("logo.txt", b"pixels")
        response = await client.get("/storage/logo.txt", follow_redirects=True)
        assert response.status_code == 404


def test_minio_infra_is_opt_in(tmp_path: Path) -> None:
    plugin = StoragePlugin({"disks": DEFAULT_DISKS})
    assert plugin.infra() == []

    plugin = StoragePlugin({"disks": DEFAULT_DISKS, "minio_include_infra": True})
    services = plugin.infra()
    assert [s.name for s in services] == ["minio"]
    assert services[0].internal_port == 9000
