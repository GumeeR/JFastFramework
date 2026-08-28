#!/usr/bin/env bash
# Generate a Go service and actually build, test and run it.
#
#     bash scripts/smoke_go.sh
#
# The point of generating a Go service is that it compiles. A template that
# has never been through `go build` is a liability that looks like a feature,
# so this runs the whole path: vet, test, build, start the binary, curl it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/jfast" ]]; then
  JFAST="${ROOT}/.venv/bin/jfast"
else
  JFAST="$(command -v jfast)"
fi

if ! command -v go > /dev/null; then
  echo "go is not installed; skipping (CI's setup-go provides it)"
  exit 0
fi

WORK="$(mktemp -d)"
PID=""
trap 'rm -rf "${WORK}"; [ -n "${PID}" ] && kill "${PID}" 2>/dev/null || true' EXIT
cd "${WORK}"

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; exit 1; }

step "generate"
"${JFAST}" workspace init poly > /dev/null
"${JFAST}" new service edge --language go --with cache --grpc
cd edge
find . -type f -not -name '.jfast-template' | sort

step "go vet"
go vet ./...

step "go test"
go test ./...

step "go build"
go build -o "${WORK}/edge-bin" .
ls -la "${WORK}/edge-bin"

step "run it"
JFAST_PORT=9123 JFAST_LOG_JSON_LOGS=true "${WORK}/edge-bin" > "${WORK}/log.txt" 2>&1 &
PID=$!
for _ in $(seq 1 40); do
  curl -fsS localhost:9123/health > /dev/null 2>&1 && break
  sleep 0.25
done

curl -fsS localhost:9123/health | grep -q '"status":"ok"' || fail "/health"
curl -fsS localhost:9123/ready | grep -q '"status":"ok"' || fail "/ready"

curl -fsS -X POST localhost:9123/items -H 'content-type: application/json' \
  -d '{"id":0,"name":"first","is_active":true}' | grep -q '"id":1' || fail "POST /items"

# Errors must be problem+json -- the same shape the Python services emit.
curl -sS -i localhost:9123/items/999 | grep -qi 'application/problem+json' \
  || fail "404 is not problem+json"

# A trace that restarts at every hop is a trace nobody can follow.
curl -sS -i -H 'X-Request-ID: trace-me' localhost:9123/health \
  | grep -qi 'x-request-id: trace-me' || fail "request id was not propagated"

# Structured logs on stdout, carrying the correlation id.
grep -q '"request_id":"trace-me"' "${WORK}/log.txt" || fail "request id missing from logs"

step "the gRPC contract was generated"
test -f proto/edge.proto || fail "no proto"
grep -q 'service Health' proto/edge.proto || fail "proto missing the health contract"

kill "${PID}"; PID=""
printf '\nGO SMOKE OK\n'
