#!/usr/bin/env bash
# Build the image the generator writes, and run it against a real PostgreSQL.
#
#     bash scripts/smoke_docker.sh
#
# This is the check that did not exist, and four defects lived in the gap: a
# requirements pin pip could not satisfy, a COPY of a file the generator never
# writes, a container that served 500s against an empty schema, and a database
# dependency that turned every route into a 422. Every other smoke script
# installs the framework from the checkout with all extras present and never
# builds an image, so none of them could see any of it.
#
# The framework is installed from PyPI, which is what a user receives. The
# Dockerfile installs dependencies before copying the source -- deliberately,
# for layer caching -- so a wheel built here would not exist yet at that step.
#
# At release time the version a generated service pins is not published yet.
# Rather than fail on exactly the commit that is correct, the pin is then
# rewritten to the newest published pre-release and the substitution is
# announced, so the Dockerfile and the entrypoint are still exercised.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/jfast" ]]; then
  JFAST="${ROOT}/.venv/bin/jfast"
  PYTHON="${ROOT}/.venv/bin/python"
else
  JFAST="$(command -v jfast)"
  PYTHON="$(command -v python3)"
fi

NET="jfast-smoke-net-$$"
DB="jfast-smoke-db-$$"
API="jfast-smoke-api-$$"
IMAGE="jfast-smoke-image-$$"
WORK="$(mktemp -d)"

cleanup() {
  code=$?
  docker rm -f "${API}" "${DB}" > /dev/null 2>&1 || true
  docker network rm "${NET}" > /dev/null 2>&1 || true
  docker rmi -f "${IMAGE}" > /dev/null 2>&1 || true
  rm -rf "${WORK}"
  exit ${code}
}
trap cleanup EXIT

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; exit 1; }

step "generate a service and its Dockerfile"
cd "${WORK}"
"${JFAST}" workspace init smoke > /dev/null
"${JFAST}" new service billing --with database > /dev/null
cd billing
"${JFAST}" deploy dockerfile > /dev/null

grep -q 'COPY pyproject.toml\* ' Dockerfile \
  || fail "COPY pyproject.toml must be optional; the generator writes no pyproject"
grep -q 'alembic upgrade head' Dockerfile || fail "the image must migrate before serving"
grep -q 'exec uvicorn' Dockerfile || fail "uvicorn must be PID 1"

step "make sure the pin resolves"
cat requirements.txt
if ! "${PYTHON}" -m pip install --dry-run --quiet --pre \
     --target "${WORK}/resolve" -r requirements.txt > /dev/null 2>&1; then
  LATEST="$("${PYTHON}" - <<'PY'
import json, urllib.request
with urllib.request.urlopen("https://pypi.org/pypi/jfastframework/json") as r:
    print(json.load(r)["info"]["version"])
PY
)"
  echo "  the pinned version is not on PyPI yet; building against ${LATEST}"
  "${PYTHON}" - "${LATEST}" <<'PY'
import re, sys
body = open('requirements.txt', encoding='utf-8').read()
body = re.sub(r'(jfastframework\[[^\]]*\])[^\s]*', r'\1==' + sys.argv[1], body)
open('requirements.txt', 'w', encoding='utf-8').write(body)
print(body.strip().splitlines()[-1])
PY
fi

step "docker build"
docker build -q -t "${IMAGE}" . > /dev/null || fail "the generated Dockerfile does not build"

step "a real PostgreSQL"
docker network create "${NET}" > /dev/null
docker run -d --name "${DB}" --network "${NET}" \
  -e POSTGRES_USER=app -e POSTGRES_PASSWORD=smoke -e POSTGRES_DB=app \
  pgvector/pgvector:pg16 > /dev/null
for _ in $(seq 1 40); do
  docker exec "${DB}" pg_isready -U app > /dev/null 2>&1 && break
  sleep 1
done
docker exec "${DB}" pg_isready -U app > /dev/null 2>&1 || fail "postgres never became ready"

step "a failed migration stops the container instead of serving"
if docker run --name "${API}" --network "${NET}" \
     -e JFAST_DB_DSN="postgresql+asyncpg://app:wrong@${DB}:5432/app" \
     "${IMAGE}" > "${WORK}/fail.log" 2>&1; then
  fail "the container served despite a failed migration"
fi
grep -qi "password authentication failed" "${WORK}/fail.log" \
  || fail "expected the real database error: $(tail -3 "${WORK}/fail.log")"
docker rm -f "${API}" > /dev/null
echo "  stopped, with the database error surfaced"

step "against a reachable database it migrates, then serves"
docker run -d --name "${API}" --network "${NET}" -p 9457:8000 \
  -e JFAST_DB_DSN="postgresql+asyncpg://app:smoke@${DB}:5432/app" \
  "${IMAGE}" > /dev/null

ready=""
for _ in $(seq 1 40); do
  if curl -fsS http://localhost:9457/health > /dev/null 2>&1; then ready="yes"; break; fi
  sleep 1
done
[ -n "${ready}" ] || { docker logs "${API}" 2>&1 | tail -20; fail "the service never answered"; }

curl -fsS http://localhost:9457/health | grep -q '"status":"ok"' || fail "/health is not ok"
curl -fsS http://localhost:9457/ready > "${WORK}/ready.json" 2>&1 \
  || { cat "${WORK}/ready.json"; fail "/ready did not answer 200"; }
grep -q '"database":{"healthy":true' "${WORK}/ready.json" \
  || fail "the database check is not healthy: $(cat "${WORK}/ready.json")"
echo "  /health and /ready both ok, database reachable"

step "alembic really ran"
docker exec "${DB}" psql -U app -d app -tAc \
  "select count(*) from information_schema.tables where table_name='alembic_version';" \
  | grep -q '^1$' || fail "no alembic_version table: the migration did not run"
echo "  alembic_version exists"

printf '\nDOCKER SMOKE OK\n'
