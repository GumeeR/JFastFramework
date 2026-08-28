#!/usr/bin/env bash
# Generate the Vue and React frontends and actually install and build them.
#
#     bash scripts/smoke_frontend.sh
#
# This job exists because of a real bug it caught: the generator's marker
# comment `/*nuevaRuta*/` sat inside a `/* ... */` block comment, whose inner
# `*/` closed the comment early and left the router file syntactically
# invalid. Every grep-based check passed -- the marker *was* there. Only
# `vite build` said otherwise.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/jfast" ]]; then
  JFAST="${ROOT}/.venv/bin/jfast"
else
  JFAST="$(command -v jfast)"
fi

if ! command -v npm > /dev/null; then
  echo "npm is not installed; skipping (CI's setup-node provides it)"
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
cd "${WORK}"

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; exit 1; }

"${JFAST}" workspace init fronts > /dev/null
"${JFAST}" new service billing --with database > /dev/null

for FW in vue react; do
  step "${FW}"
  "${JFAST}" new service "app_${FW}" --kind spa --frontend "${FW}" > /dev/null
  cd "${WORK}/app_${FW}"
  "${JFAST}" new view Facturas > /dev/null

  echo "--- npm install ---"
  npm install --no-audit --no-fund 2>&1 | tail -2

  echo "--- npm run build ---"
  npm run build 2>&1 | tail -6

  test -f dist/index.html || fail "${FW}: no dist/index.html"

  # The production build must use the relative /api base, not the dev port.
  # Behind Caddy the API lives at /api whether or not a gateway exists, so one
  # build works in every environment — and a baked-in http://localhost:8010
  # would be a white screen in production.
  grep -q '"/api"' dist/assets/*.js || fail "${FW}: production build is not using /api"
  # Match a port, not bare "localhost": Vue's own bundle contains
  # `window.location.href || "http://localhost"` as a fallback, which is not
  # our configuration. A dev *port* is.
  if grep -qE 'localhost:[0-9]{4}' dist/assets/*.js; then
    fail "${FW}: a development URL was baked into the production build"
  fi

  # The generated module must be in the bundle: a route that silently fails to
  # register still builds, and only shows up as a blank page.
  grep -q 'Facturas' dist/assets/*.js || fail "${FW}: the generated view is not in the bundle"

  cd "${WORK}"
done

printf '\nFRONTEND SMOKE OK\n'
