#!/usr/bin/env bash
# `jfast start` end to end: the opinionated default stack, generated and run.
#
#     bash scripts/smoke_start.sh
#
# One command has to produce a coherent system, not four folders that happen
# to be adjacent. This checks the seams: the frontend's API URL, Caddy's
# routes, compose's services, the migration wiring and the starter module's
# tests.
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

step "jfast start"
"${JFAST}" start shop --port 8000

step "the monolith carries database, cache and queue"
grep -q 'enabled = \["observability", "metrics", "database", "cache", "queue"\]' shop/jfast.toml \
  || fail "plugin list: $(grep enabled shop/jfast.toml)"
grep -q 'backend = "postgres"' shop/jfast.toml || fail "queue backend not written"
test -f shop/alembic.ini || fail "no alembic.ini"
test -d shop/modules/item || fail "no starter module"

step "the frontend points at the backend"
grep -q 'VITE_API_URL=http://localhost:8000' shop-web/.env || fail "dev .env: $(cat shop-web/.env)"
grep -q 'VITE_API_URL=/api' shop-web/.env.production || fail "production .env is not relative"

step "Caddy routes the workspace"
grep -q 'handle /api/\* {' Caddyfile || fail "no /api handler"
grep -q 'reverse_proxy shop:8000' Caddyfile || fail "Caddy does not reach the monolith"
grep -q 'try_files {path} /index.html' Caddyfile || fail "SPA refresh would 404"

step "compose covers the monolith and its datastores"
grep -q '^  shop:$' docker-compose.yml || fail "no monolith service"
grep -q 'shop-database' docker-compose.yml || fail "no postgres"
grep -q 'shop-cache' docker-compose.yml || fail "no redis"
grep -q 'image: "caddy' docker-compose.yml || fail "no caddy"
# A pinned container_name is global to the daemon, so a second copy of this
# workspace could not start beside the first. Compose derives the name from
# the project (the directory, or `docker compose -p <name>`).
if grep -q 'container_name' docker-compose.yml; then
  fail "a container name is pinned"
fi
# A built SPA is static files; Caddy serves them. Running a Node container in
# production to serve a dist/ folder is a process nobody needs.
if grep -q '^  shop-web:$' docker-compose.yml; then
  fail "the SPA should not be a container"
fi

step "the monolith imports, tests and migrates"
cd shop
PYTHONPATH="${WORK}/shop" "${PY}" -m compileall -q modules main.py
PYTHONPATH="${WORK}/shop" "${PY}" -m pytest -q modules
JFAST_DB_DSN="postgresql+asyncpg://u:p@localhost:5432/db" \
  PYTHONPATH="${WORK}/shop" "${PY}" -m alembic upgrade head --sql > /dev/null

step "the frontend builds"
cd "${WORK}/shop-web"
if command -v npm > /dev/null; then
  npm install --no-audit --no-fund > /dev/null 2>&1
  npm run build 2>&1 | tail -3
  test -f dist/index.html || fail "no dist/index.html"
else
  echo "npm is not installed; skipping the build (CI's setup-node provides it)"
fi

printf '\nSTART SMOKE OK\n'
