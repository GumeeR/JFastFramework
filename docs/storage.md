# Storage

Files live on a **named disk**. Your code writes to `"public"` or `"invoices"`;
whether that disk is a directory, an S3 bucket or a MinIO container is
configuration. The same handler works in development and in production without
an edit.

This is Laravel's disk idea, and it is worth copying: the alternative — a
handler that knows it is writing to `/var/app/uploads` — cannot be deployed
anywhere else without a rewrite.

```bash
jfast new service billing --plugins storage
```

## The two default disks

A new service gets two, and the difference between them is the whole point:

| Disk | Visibility | URL | Used for |
| --- | --- | --- | --- |
| `public` | `public` | permanent | avatars, logos, anything already public |
| `private` | `private` | expires | invoices, contracts, exports, uploads |

```toml
[plugin.storage]
default = "public"
serve_local = true                    # development only

[plugin.storage.disks.public]
driver = "local"
root = "storage/public"
visibility = "public"

[plugin.storage.disks.private]
driver = "local"
root = "storage/private"
visibility = "private"
```

A private disk **refuses** to produce a permanent URL. That is not a
convenience check — a permanent URL to a private disk is exactly how invoice
PDFs end up in a search index.

```python
storage = request.app.state.jfast.require("storage")

await storage.disk("public").put("logos/acme.png", data, content_type="image/png")
storage.disk("public").url("logos/acme.png")          # /storage/public/logos/acme.png

await storage.disk("private").put("invoices/1042.pdf", pdf)
storage.disk("private").url("invoices/1042.pdf")      # StorageError
await storage.disk("private").temporary_url("invoices/1042.pdf", expires_in=300)
```

## Temporary URLs

For a local disk the link carries an expiry and an HMAC signature over **both**
the key and that expiry:

```
/storage/private/invoices/1042.pdf?expires=1793491200&signature=Yk3f...
```

Signing the key alone would make one valid link a key to the entire disk; the
holder could edit the path. Signing the expiry alone would let them edit the
path instead. The signature covers both, and comparison is constant-time.

Set the key or temporary URLs do not work:

```bash
JFAST_STORAGE_SIGNING_KEY=$(openssl rand -hex 32)
```

The plugin warns at startup when a private local disk has no key, rather than
letting the first user to click a download link discover it.

An expired link and a forged one return the same 403 with the same message.
Different answers would tell an attacker whether the key exists.

For an S3 disk, `temporary_url()` is a presigned URL and the signing key is
irrelevant — S3 does the signing with its own credentials.

## S3 and MinIO

```toml
[plugin.storage.disks.uploads]
driver = "s3"
bucket = "acme-uploads"
region = "eu-west-1"
visibility = "private"
```

No credentials in the config: boto3's default chain finds the instance role,
the task role, or `~/.aws/credentials`. A task role is better than any key you
could put here, because there is no long-lived secret to leak.

MinIO is S3 with two extra settings:

```toml
[plugin.storage.disks.uploads]
driver = "s3"
bucket = "uploads"
endpoint_url = "http://localhost:8006"
access_key = "jfast"
secret_key = "${MINIO_PASSWORD}"
force_path_style = true              # MinIO needs it, real S3 does not
```

To get the container in your compose file:

```toml
[plugin.storage]
minio_include_infra = true
```

`jfast deploy compose` then emits MinIO at your service's base port `+6`, like
every other plugin's container.

## Keys are not paths

Every backend runs the key through the same validator before it reaches a
filesystem or a bucket:

- no `..`, no leading `/`, no backslashes, no null bytes;
- `.` and `..` resolved *before* the check, so `a/../../b` is caught here
  rather than by the filesystem;
- letters, digits, `.`, `_`, `-` and `/` only;
- 1024 characters maximum.

Local disks additionally re-check after resolving, because a symlink inside the
disk root can point outside it and only resolution reveals that.

Path traversal is the most common storage vulnerability there is. Validating in
the backend rather than in the plugin means a backend used directly — from a
worker, from a script — is as safe as one used through a route.

## Serving files

`serve_local = true` mounts `/storage/{disk}/{key}` so downloads work with
nothing else running. It is for development.

In production, put Caddy or a CDN in front of the public disk and set
`serve_local = false`. A Python worker holding a connection open to stream a
40 MB PDF is a worker not serving requests. The plugin logs a warning if it
finds itself serving files in production.

Downloads are sent as `Content-Disposition: attachment` with
`X-Content-Type-Options: nosniff`. An uploaded `.html` or `.svg` rendered
inline runs the uploader's script on your origin, against your users' cookies —
so uploads are saved, not rendered. Pass `inline=True` to
`sanitised_download_headers()` only for files your own code produced.

## Health

`/ready` reports each disk: a local disk that is missing or read-only, an S3
bucket that cannot be reached. Storage failing does not make the service
unhealthy on its own — an API that can still answer queries should stay in the
load balancer — so it is reported as degraded.

## What this does not do

- **No image processing.** Thumbnails, resizing and format conversion belong in
  a job, not in a storage layer.
- **No streaming uploads.** `put()` takes bytes. A multi-gigabyte upload should
  go straight to S3 with a presigned `upload_url()` and never pass through the
  application at all.
- **No virus scanning.** If you accept uploads from the public, you need it;
  it is not here.

## See also

- [Multi-tenancy](multitenancy.md) — per-tenant prefixes and subdomains
- [Deployment](deploy.md) — Caddy in front of the public disk
