#!/usr/bin/env bash
# Auth and Kubernetes, end to end on a generated service.
#
#     bash scripts/smoke_auth_k8s.sh
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

step "a service with auth"
"${JFAST}" workspace init secure > /dev/null
"${JFAST}" new service billing --with database,cache,auth > /dev/null
cd billing
grep -q '"auth"' jfast.toml || fail "auth not in the plugin list"
grep -q 'JFAST_AUTH_JWKS_URL' .env.example || fail "auth env keys missing"
grep -q 'audience = "billing"' jfast.toml || fail "audience not defaulted to the service name"

step "it boots, guards a route, and refuses the classic attacks"
PYTHONPATH="${WORK}/billing" JFAST_AUTH_SECRET="a-secret-long-enough-for-sha256-32-bytes-plus" \
  "${PY}" - <<'PYEOF'
import asyncio
from datetime import timedelta

import httpx
from fastapi import APIRouter, Depends
from httpx import ASGITransport

from jfastframework.auth import Principal, issue, require_scopes
from jfastframework.testing import build_test_app

SECRET = "a-secret-long-enough-for-sha256-32-bytes-plus"
router = APIRouter()


@router.get("/invoices")
async def invoices(caller: Principal = Depends(require_scopes("invoices:read"))):
    return {"subject": caller.subject, "tenant": caller.tenant_id}


# The generated jfast.toml defaults to `jwks`, which needs an identity service.
# This smoke uses the HMAC mode so it runs standalone -- the verification path
# under test is the same either way.
app = build_test_app(
    plugins=["auth"],
    app_name="billing",
    routers=[router],
    raw={
        "plugin": {
            "auth": {
                "mode": "secret",
                "secret": SECRET,
                "algorithms": ["HS256"],
                "issuer": "https://id.test/",
                "audience": "billing",
            }
        }
    },
)


def mint(**kw):
    token, _, _ = issue(
        kw.pop("subject", "u1"),
        key=SECRET,
        algorithm="HS256",
        lifetime=kw.pop("lifetime", timedelta(minutes=5)),
        audience="billing",
        issuer="https://id.test/",
        **kw,
    )
    return token


async def go() -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        async with app.router.lifespan_context(app):
            def bearer(t):
                return {"Authorization": f"Bearer {t}"}

            r = await c.get("/invoices")
            assert r.status_code == 401, r.status_code

            r = await c.get("/invoices", headers=bearer(mint(scopes=["invoices:read"], tenant_id="acme")))
            assert r.status_code == 200, r.text
            assert r.json() == {"subject": "u1", "tenant": "acme"}, r.json()

            r = await c.get("/invoices", headers=bearer(mint(scopes=["invoices:write"])))
            assert r.status_code == 403, r.status_code

            # Wrong audience: a token minted for a sibling service.
            token, _, _ = issue("u1", key=SECRET, algorithm="HS256",
                                lifetime=timedelta(minutes=5), audience="other",
                                issuer="https://id.test/", scopes=["invoices:read"])
            r = await c.get("/invoices", headers=bearer(token))
            assert r.status_code == 401, r.status_code

            # A forged tenant header must not beat the signed claim.
            r = await c.get(
                "/invoices",
                headers={**bearer(mint(scopes=["invoices:read"], tenant_id="acme")),
                         "X-Tenant-ID": "attacker"},
            )
            assert r.json()["tenant"] == "acme", r.json()

            # Logout revokes.
            good = mint(scopes=["invoices:read"])
            assert (await c.get("/invoices", headers=bearer(good))).status_code == 200
            await c.post("/auth/logout", headers=bearer(good))
            assert (await c.get("/invoices", headers=bearer(good))).status_code == 401
    print("auth OK")


asyncio.run(go())
PYEOF

step "kubernetes manifests"
cd "${WORK}"
"${JFAST}" new service catalog --with database > /dev/null
"${JFAST}" workspace k8s --host app.example.com | tail -6

test -f k8s/base/kustomization.yaml || fail "no kustomization"
test -f k8s/base/billing.yaml || fail "no billing manifest"
test -f k8s/overlays/prod/kustomization.yaml || fail "no prod overlay"
grep -q 'path: /health' k8s/base/billing.yaml || fail "no liveness on /health"
grep -q 'path: /ready' k8s/base/billing.yaml || fail "no readiness on /ready"
grep -q 'runAsNonRoot: true' k8s/base/billing.yaml || fail "pods would run as root"
if grep -rq 'StatefulSet' k8s/base/*.yaml; then
  fail "a database StatefulSet was generated"
fi

step "the manifests parse"
"${PY}" - <<'PYEOF'
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("  pyyaml not installed; skipped")
    sys.exit(0)

count = 0
for path in sorted(Path("k8s").rglob("*.yaml")):
    for document in yaml.safe_load_all(path.read_text()):
        if document:
            assert "kind" in document, path
            count += 1
print(f"  {count} manifests parsed")
PYEOF

printf '\nAUTH + K8S SMOKE OK\n'
