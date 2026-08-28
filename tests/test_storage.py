"""Storage: keys that must be rejected, and links that must expire.

Most of this file is about the two ways a storage layer leaks: a key that
escapes its disk, and a private file reachable without a valid signature.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from jfastframework.errors import PluginError
from jfastframework.plugins.builtin.storage import DEFAULT_DISKS, StoragePlugin
from jfastframework.storage.base import (
    InvalidKey,
    normalise_key,
    sanitised_download_headers,
)
from jfastframework.storage.local import FileNotFound, LocalStorage, StorageError
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
    (tmp_path / "files" / "link").symlink_to(outside)
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


def test_minio_infra_is_opt_in(tmp_path: Path) -> None:
    plugin = StoragePlugin({"disks": DEFAULT_DISKS})
    assert plugin.infra() == []

    plugin = StoragePlugin({"disks": DEFAULT_DISKS, "minio_include_infra": True})
    services = plugin.infra()
    assert [s.name for s in services] == ["minio"]
    assert services[0].internal_port == 9000
