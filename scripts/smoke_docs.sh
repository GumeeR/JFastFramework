#!/usr/bin/env bash
# Run exactly the commands docs/local-setup.md tells a new user to run.
#
#     bash scripts/smoke_docs.sh
#
# Documentation that has never been executed is a guess. This is the check
# that the quickstart still works for someone arriving today -- including the
# part where the service starts without a database and says so.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/jfast" ]]; then
  export PATH="${ROOT}/.venv/bin:${PATH}"
fi

WORK="$(mktemp -d)"
# Preserve the failing status: a trap whose last command succeeds would
# otherwise hand its own exit code to the script, and a failed smoke run
# would report success in CI.
trap 'code=$?; rm -rf "${WORK}"; exit ${code}' EXIT

step() { printf '\n=== %s ===\n' "$1"; }

step "jfast version"
jfast version

step "mkdir + jfast start, exactly as documented"
mkdir -p "${WORK}/shop" && cd "${WORK}/shop"
jfast start shop | tail -22

step "the documented tree exists"
for EXPECTED in \
  shop/modules/item shop/contracts.toml shop/alembic.ini \
  shop-web/package.json docker-compose.yml Caddyfile jfast.workspace.toml; do
  test -e "${EXPECTED}" || { echo "MISSING ${EXPECTED}"; exit 1; }
  echo "  ok  ${EXPECTED}"
done

step "backend: pytest, then contracts check"
cd shop
PYTHONPATH="${WORK}/shop/shop" python -m pytest -q 2>&1 | tail -3
jfast contracts check

step "backend: it imports and serves /health"
PYTHONPATH="${WORK}/shop/shop" python - <<'PYEOF'
import asyncio

import httpx
from httpx import ASGITransport

import main


async def go() -> None:
    transport = ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with main.app.router.lifespan_context(main.app):
            response = await client.get("/health")
            assert response.status_code == 200, response.text
            print("  /health ->", response.json())


asyncio.run(go())
PYEOF

step "the documented module loop"
jfast new module invoice | tail -6
PYTHONPATH="${WORK}/shop/shop" python -m pytest -q modules/invoice/tests 2>&1 | tail -2
jfast contracts check

step "frontend: npm install && npm run build"
cd ../shop-web
if command -v npm > /dev/null; then
  npm install --no-audit --no-fund > /dev/null 2>&1
  npm run build 2>&1 | tail -3
else
  echo "  npm not on PATH; skipped"
fi

printf '\nDOCS VERIFIED\n'
