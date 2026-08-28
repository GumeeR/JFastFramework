#!/usr/bin/env bash
# Storage, multi-tenancy and the serverless artifacts, end to end on a
# generated service.
#
#     bash scripts/smoke_storage_tenancy.sh
#
# Greps are not enough here and this file exists because of that: a generated
# service can pass every grep and still fail to boot. So it boots, writes a
# file, and fetches it over HTTP.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"; JFAST="${ROOT}/.venv/bin/jfast"
else
  PY="$(command -v python3 || command -v python)"; JFAST="$(command -v jfast)"
fi

WORK="$(mktemp -d)"
# Preserve the failing status: a trap whose last command succeeds would
# otherwise hand its own exit code to the script, and a failed smoke run
# would report success in CI.
trap 'code=$?; rm -rf "${WORK}"; exit ${code}' EXIT
cd "${WORK}"

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; exit 1; }

step "a service with storage and tenancy"
"${JFAST}" workspace init saas > /dev/null
"${JFAST}" new service billing --with database,storage,tenancy,notifications > /dev/null
cd billing
grep -q '"storage"' jfast.toml || fail "storage not in the plugin list"
grep -q '"tenancy"' jfast.toml || fail "tenancy not in the plugin list"

step "the generated jfast.toml actually boots"
# This step exists because it caught a real one: the config template had no
# [plugin.tenancy] block, so a generated service with tenancy enabled failed
# at import with "subdomain needs base_domain". Every grep passed.
grep -q '\[plugin.storage.disks.private\]' jfast.toml || fail "no private disk in jfast.toml"
grep -q 'base_domain' jfast.toml || fail "no base_domain in jfast.toml"
grep -q 'JFAST_STORAGE_SIGNING_KEY' .env.example || fail "no signing key in .env.example"
PYTHONPATH="${WORK}/billing" JFAST_STORAGE_SIGNING_KEY=smoke-key "${PY}" - <<'PYEOF'
import main

assert main.app.title, "the generated app has no title"
print("the generated service boots")
PYEOF

step "it boots, serves a public file, and refuses an unsigned private one"
PYTHONPATH="${WORK}/billing" "${PY}" - <<'PYEOF'
import asyncio

import httpx
from httpx import ASGITransport

from jfastframework.testing import build_test_app

app = build_test_app(
    plugins=["observability", "storage", "tenancy"],
    raw={
        "plugin": {
            "storage": {
                "default": "public",
                "signing_key": "smoke-test-signing-key-32-bytes-minimum",
                "disks": {
                    "public": {"driver": "local", "root": "storage/public",
                               "visibility": "public"},
                    "private": {"driver": "local", "root": "storage/private",
                                "visibility": "private"},
                },
            },
            "tenancy": {"sources": ["subdomain"], "base_domain": "app.example.com"},
        }
    },
)


async def main() -> None:
    transport = ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        storage = app.state.jfast.require("storage")
        await storage.disk("public").put("logo.txt", b"pixels")
        await storage.disk("private").put("invoice.txt", b"secret")

        public = await client.get("/storage/public/logo.txt")
        assert public.status_code == 200, public.status_code
        assert public.content == b"pixels"
        # An uploaded file must never be rendered inline from our origin.
        assert public.headers["content-disposition"].startswith("attachment")
        assert public.headers["x-content-type-options"] == "nosniff"

        naked = await client.get("/storage/private/invoice.txt")
        assert naked.status_code == 403, naked.status_code

        signed = await storage.disk("private").temporary_url("invoice.txt", expires_in=60)
        ok = await client.get(signed)
        assert ok.status_code == 200, ok.status_code
        assert ok.content == b"secret"

        forged = await client.get(
            "/storage/private/invoice.txt?expires=99999999999&signature=nope"
        )
        assert forged.status_code == 403, forged.status_code

        traversal = await client.get("/storage/public/..%2f..%2fetc%2fpasswd")
        assert traversal.status_code == 404, traversal.status_code

        ready = await client.get("/ready")
        assert ready.status_code == 200, ready.text
        assert "storage" in ready.json()["checks"], ready.text

        # A reserved subdomain and the bare base domain are not tenants, and
        # neither is allowed to break a request.
        for host in ("acme.app.example.com", "app.example.com", "www.app.example.com"):
            response = await client.get("/health", headers={"host": host})
            assert response.status_code == 200, response.status_code

    print("storage and tenancy OK")


asyncio.run(main())
PYEOF

step "a wildcard Caddyfile gates on-demand TLS"
cd "${WORK}"
"${JFAST}" workspace caddy --hostname app.example.com --production \
  --wildcard-tenants --output Caddyfile.tenants > /dev/null
grep -q '\*\.app\.example\.com' Caddyfile.tenants || fail "no wildcard site block"
grep -q 'on_demand_tls' Caddyfile.tenants || fail "no on-demand TLS block"
# Without `ask`, anyone pointing DNS at us can burn the certificate rate limit.
grep -q 'ask http' Caddyfile.tenants || fail "on-demand TLS has no ask endpoint"

step "serverless artifacts are private by default"
cd "${WORK}/billing"
"${JFAST}" deploy function billing --target aws --account-id 123456789012 \
  --output "${WORK}/fn" > /dev/null
grep -q 'platform linux/amd64' "${WORK}/fn/deploy-lambda.sh" \
  || fail "the Lambda build does not pin the platform"
grep -q 'auth-type AWS_IAM' "${WORK}/fn/deploy-lambda.sh" \
  || fail "the Function URL is not private by default"
grep -q 'Mangum' "${WORK}/fn/handler.py" || fail "no Mangum handler"
[[ -x "${WORK}/fn/deploy-lambda.sh" ]] || fail "the deploy script is not executable"
bash -n "${WORK}/fn/deploy-lambda.sh" || fail "the generated Lambda script is not valid bash"

"${JFAST}" deploy function billing --target gcp --project my-project \
  --output "${WORK}/fn-gcp" > /dev/null
grep -q 'no-allow-unauthenticated' "${WORK}/fn-gcp/deploy-cloudrun.sh" \
  || fail "the Cloud Run service is not private by default"
bash -n "${WORK}/fn-gcp/deploy-cloudrun.sh" || fail "the generated gcloud script is not valid bash"

printf '\nAll storage / tenancy / serverless checks passed.\n'
