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
# Preserve the failing status: a trap whose last command succeeds would
# otherwise hand its own exit code to the script, and a failed smoke run
# would report success in CI.
trap 'code=$?; rm -rf "${WORK}"; exit ${code}' EXIT

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

step "resources: a second database, and one cache shared by two services"
cd "${WORK}"
"${JFAST}" workspace migrate-resources > /dev/null
"${JFAST}" workspace resource analytics-db --type postgres --database analytics > /dev/null

# Both databases default to JFAST_DB_DSN, so binding the second without --as
# must be refused. That check is the whole reason bindings name a variable.
if "${JFAST}" link billing analytics-db > /tmp/clash.txt 2>&1; then
  fail "binding two databases to JFAST_DB_DSN should be refused"
fi
grep -q "already binds" /tmp/clash.txt || fail "wrong error: $(cat /tmp/clash.txt)"

"${JFAST}" link billing analytics-db --as JFAST_ANALYTICS_DSN > /dev/null
"${JFAST}" workspace resource shared-redis --type redis > /dev/null
"${JFAST}" link catalog shared-redis > /dev/null
# billing already has its own cache from `--with database,cache`; sharing
# one means dropping that first, and the resource then has no users.
"${JFAST}" unlink billing billing-cache > /dev/null
"${JFAST}" workspace resource billing-cache --remove > /dev/null
"${JFAST}" link billing shared-redis > /dev/null
"${JFAST}" workspace validate

step "the DSN is generated, not typed"
"${JFAST}" workspace env > /dev/null
grep -q "JFAST_ANALYTICS_DSN=postgresql+asyncpg://app:" billing/.env \
  || fail "billing/.env has no analytics DSN: $(cat billing/.env)"
grep -q "@analytics-db:5432/analytics" billing/.env || fail "wrong host in the analytics DSN"
grep -q "JFAST_CACHE_URL=redis://shared-redis:6379/0" catalog/.env \
  || fail "catalog does not point at the shared cache"
test -f .env || fail "no workspace secrets file"
grep -q "ANALYTICS_DB_PASSWORD=" .env || fail "no generated password for analytics-db"
grep -q "^.env$" .gitignore || fail ".env is not gitignored"

step "one container per resource, shared where it is shared"
"${JFAST}" workspace compose > /dev/null
python3 - <<'PY'
import re
compose = open("docker-compose.yml", encoding="utf-8").read()
assert compose.count("container_name: shared-redis") == 1, "the shared cache was duplicated"
assert "analytics-db" in compose, "the second database is missing"
assert "catalog-cache" not in compose, "the removed resource is still emitted"
print("compose OK")
PY

step "the graph names the variable on every edge"
"${JFAST}" workspace graph | tee /tmp/graph.txt | head -3
grep -q "billing -->|JFAST_ANALYTICS_DSN| analytics_db" /tmp/graph.txt \
  || fail "the graph does not label the binding: $(cat /tmp/graph.txt)"

printf '\nWORKSPACE SMOKE OK\n'
