#!/usr/bin/env bash
# Run the compose file the generators write, the way the panel says to run it.
#
#     bash scripts/smoke_compose.sh
#
# `smoke_docker.sh` proves the image is correct, but it builds that image with
# `docker build` and wires the network, the database and the environment by
# hand -- which is precisely the work the generated compose file exists to do.
# So every defect that lived in the compose file survived a green smoke run:
#
#   * no generator wrote a Dockerfile, and both compose files say `build:`, so
#     `docker compose up --build` stopped at "failed to read dockerfile";
#   * the api service loaded a .env whose DSN said `localhost`, which inside a
#     container is that container, so it crash-looped against its own port;
#   * `jfast start` rendered a module and never mounted it, so the endpoints
#     the generated tests covered did not exist at runtime.
#
# Nothing here inspects a file. It runs the two commands a new user runs and
# asks the running containers what they are serving. Both generators are
# covered because they fail differently: `jfast start` writes a workspace, and
# `jfast new service` plus `jfast deploy compose` writes a single service.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/jfast" ]]; then
  JFAST="${ROOT}/.venv/bin/jfast"
  PYTHON="${ROOT}/.venv/bin/python"
else
  JFAST="$(command -v jfast)"
  PYTHON="$(command -v python3)"
fi

WORK="$(mktemp -d)"
# The compose project name, so teardown reaches every container even when the
# run dies between `up` and the first assertion.
WORKSPACE_PROJECT="jfastworkspace$$"
SERVICE_PROJECT="jfastservice$$"
# Above the default block and above anything smoke_docker.sh publishes, so two
# jobs on one runner do not collide on a host port.
BASE_PORT=9600

cleanup() {
  code=$?
  for project in "${WORKSPACE_PROJECT}" "${SERVICE_PROJECT}"; do
    docker compose -p "${project}" down -v --remove-orphans > /dev/null 2>&1 || true
  done
  docker rmi -f "${WORKSPACE_PROJECT}-demo" "${SERVICE_PROJECT}-api" > /dev/null 2>&1 || true
  rm -rf "${WORK}"
  exit ${code}
}
trap cleanup EXIT

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; exit 1; }

# At release time the version a generated service pins is not published yet, and
# the image installs from PyPI. Rather than fail on exactly the commit that is
# correct, the pin is rewritten to the newest published pre-release -- the same
# trade smoke_docker.sh makes, and for the same reason. Announced, never silent.
resolve_pin() {
  if "${PYTHON}" -m pip install --dry-run --quiet --pre \
       --target "${WORK}/resolve" -r requirements.txt > /dev/null 2>&1; then
    return 0
  fi
  local latest
  latest="$("${PYTHON}" - <<'PY'
import json, urllib.request
with urllib.request.urlopen("https://pypi.org/pypi/jfastframework/json") as r:
    print(json.load(r)["info"]["version"])
PY
)"
  echo "  the pinned version is not on PyPI yet; building against ${latest}"
  "${PYTHON}" - "${latest}" <<'PY'
import re, sys
body = open('requirements.txt', encoding='utf-8').read()
open('requirements.txt', 'w', encoding='utf-8').write(
    re.sub(r'(jfastframework\[[^\]]*\])[^\s]*', r'\1==' + sys.argv[1], body)
)
PY
}

# Ask the running container rather than the host: the host port is a mapping
# that may be absent, and what is being tested is the network the compose file
# built. `python` is in the image because the image is a Python service.
inside() {
  local project="$1" service="$2" port="$3" path="$4"
  docker compose -p "${project}" exec -T "${service}" python - "${port}" "${path}" <<'PY'
import sys, urllib.request
port, path = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
    sys.stdout.write(response.read().decode())
PY
}

wait_for_ready() {
  local project="$1" service="$2" port="$3"
  for _ in $(seq 1 60); do
    if inside "${project}" "${service}" "${port}" /ready > /dev/null 2>&1; then
      return 0
    fi
    if [ -z "$(docker compose -p "${project}" ps -q "${service}")" ]; then
      break
    fi
    sleep 2
  done
  docker compose -p "${project}" logs --tail 40 "${service}" || true
  fail "${service} never became ready"
}


# -- the flagship command, end to end -----------------------------------

step "jfast start, then the first line of its own panel"
cd "${WORK}"
mkdir workspace && cd workspace
"${JFAST}" start demo --port "${BASE_PORT}" > /dev/null

[ -f demo/Dockerfile ] || fail "no demo/Dockerfile: \`build:\` in the compose file has nothing to read"

(cd demo && resolve_pin)

docker compose -p "${WORKSPACE_PROJECT}" up -d --build demo \
  || fail "docker compose up --build failed on a project nobody had touched"
wait_for_ready "${WORKSPACE_PROJECT}" demo "${BASE_PORT}"

step "the datastores are reachable from inside the network"
inside "${WORKSPACE_PROJECT}" demo "${BASE_PORT}" /ready | grep -q '"status":"ok"' \
  || fail "/ready is not ok: the api container cannot reach a datastore it depends on"

step "the module jfast start generated is actually served"
# The failure this catches is silent: the module exists, its tests pass, and
# the routes are absent because nothing mounted the router.
inside "${WORKSPACE_PROJECT}" demo "${BASE_PORT}" /openapi.json | grep -q '"/items"' \
  || fail "/items is not in the schema: the generated module was never mounted"

docker compose -p "${WORKSPACE_PROJECT}" down -v > /dev/null 2>&1 || true


# -- the single-service path, run exactly as the panel prints it --------

step "jfast new service, then deploy compose, then up"
cd "${WORK}"
mkdir service && cd service
"${JFAST}" new service billing --port $((BASE_PORT + 20)) > /dev/null
cd billing
# Copied rather than edited: the .env is what a user has at this point, and the
# addresses in it are the host's. The compose file has to override them.
cp .env.example .env
"${JFAST}" deploy compose -o docker-compose.yml > /dev/null
resolve_pin

docker compose -p "${SERVICE_PROJECT}" up -d --build \
  || fail "docker compose up failed on the file jfast deploy compose just wrote"
wait_for_ready "${SERVICE_PROJECT}" api $((BASE_PORT + 20))

inside "${SERVICE_PROJECT}" api $((BASE_PORT + 20)) /ready | grep -q '"status":"ok"' \
  || fail "/ready is not ok: the api container is reading the host's DSN"

step "every check passed"
