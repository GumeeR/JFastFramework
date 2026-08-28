#!/usr/bin/env bash
# Multi-service smoke check: workspace, datastore selection, gateway, frontend.
#
#     bash scripts/smoke_workspace.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"; JFAST="${ROOT}/.venv/bin/jfast"
else
  PY="$(command -v python3 || command -v python)"; JFAST="$(command -v jfast)"
fi
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; exit 1; }

cd "${WORK}"

step "workspace init"
"${JFAST}" workspace init cometax

step "backend #1 with database + cache"
"${JFAST}" new service billing --with database,cache
grep -q 'enabled = \["observability", "metrics", "database", "cache"\]' billing/jfast.toml \
  || fail "plugin list not written from --with"
grep -q 'jfastframework\[.*cache.*\]' billing/requirements.txt || fail "extras not derived"
test -f billing/alembic.ini || fail "no alembic.ini"
test -f billing/migrations/env.py || fail "no migrations/env.py"
test -f billing/migrations/script.py.mako || fail "no alembic script template"
test -f billing/pytest.ini || fail "no pytest.ini"
echo "backend #1 OK"

step "no gateway yet (one backend does not need one)"
"${JFAST}" workspace gateway | tee /tmp/gw.txt
grep -q "skipping" /tmp/gw.txt || fail "gateway should be skipped with one backend"

step "backend #2 with qdrant + rag — gateway should appear automatically"
"${JFAST}" new service catalog --with qdrant,rag
grep -q 'store = "qdrant"' catalog/jfast.toml || fail "rag store not set to qdrant"
test -f gateway/jfast.toml || fail "gateway was not generated automatically"
grep -q 'prefix = "/billing"' gateway/jfast.toml || fail "gateway missing billing route"
grep -q 'prefix = "/catalog"' gateway/jfast.toml || fail "gateway missing catalog route"
echo "auto-gateway OK"

step "ports are allocated in non-overlapping blocks"
"${JFAST}" workspace list

step "frontend (vue) — .env points at the gateway, not at a backend"
"${JFAST}" new service admin --kind spa --frontend vue
GATEWAY_PORT="$(grep -A3 'name = "gateway"' jfast.workspace.toml | grep '^port' | tr -dc '0-9')"
grep -q "VITE_API_URL=http://localhost:${GATEWAY_PORT}" admin/.env \
  || fail "frontend .env does not point at the gateway (:${GATEWAY_PORT})"
echo "frontend env OK"

step "vue view generation + router/menu patching"
cd admin
"${JFAST}" new view Facturas
test -f "src/ModuloFacturas/Pages/FacturasView.vue" || fail "page missing"
test -f "src/ModuloFacturas/Routes/router.js" || fail "routes missing"
test -f "src/ModuloFacturas/Services/facturas.service.js" || fail "service missing"
test -d "src/ModuloFacturas/Components/Modals" || fail "Modals folder missing"
grep -q "ModuloFacturas" src/router/index.js || fail "router not patched"
grep -q "'/facturas'" src/menuAside.js || fail "menu not patched"
grep -q "mdiViewDashboardOutline" src/menuAside.js || fail "icon import not added"
grep -q "/\*nuevaRuta\*/" src/router/index.js || fail "router marker not preserved"
grep -q "/\*nuevoModulo\*/" src/menuAside.js || fail "menu marker not preserved"
grep -q "{{ item.name }}" "src/ModuloFacturas/Pages/FacturasView.vue" \
  || fail "Vue runtime interpolation was consumed at scaffold time"
echo "vue view OK"

step "generator is idempotent"
"${JFAST}" new view Facturas > /dev/null
test "$(grep -c "ModuloFacturas" src/router/index.js)" -eq 2 \
  || fail "duplicate route registration (expected import + spread only)"
test "$(grep -c "'/facturas'" src/menuAside.js)" -eq 1 \
  || fail "duplicate sidebar entry"
echo "idempotent OK"

step "react frontend + view"
cd "${WORK}"
"${JFAST}" new service portal --kind spa --frontend react --port 8090
cd portal
"${JFAST}" new view Reportes
test -f "src/ModuloReportes/Pages/ReportesView.jsx" || fail "react page missing"
grep -q "ModuloReportes" src/router/index.jsx || fail "react router not patched"
grep -q "{item.name}" "src/ModuloReportes/Pages/ReportesView.jsx" \
  || fail "JSX braces consumed at scaffold time"
echo "react view OK"

step "gateway service boots and reports its routes"
cd "${WORK}/gateway"
"${PY}" - <<'PYEOF'
import asyncio

import httpx
from httpx import ASGITransport

import main


async def go() -> None:
    transport = ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with main.app.router.lifespan_context(main.app):
            health = await client.get("/health")
            assert health.status_code == 200, health.text
            routes = await client.get("/gateway/routes")
            assert routes.status_code == 200, routes.text
            prefixes = [r["prefix"] for r in routes.json()["routes"]]
            assert prefixes == ["/billing", "/catalog"], prefixes
            # An unreachable upstream must be 502 problem+json, not a stack trace.
            proxied = await client.get("/billing/anything")
            assert proxied.status_code == 502, proxied.status_code
            assert proxied.json()["title"] == "Bad Gateway"
    print("gateway OK", prefixes)


asyncio.run(go())
PYEOF

printf '\nWORKSPACE SMOKE OK\n'
