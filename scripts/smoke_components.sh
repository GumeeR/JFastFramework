#!/usr/bin/env bash
# The generated frontends must build.
#
# Rendering a template proves the braces were right. It does not prove the
# component imports something that exists, that the store it reaches for is the
# one the host renders, or that JSX with a typo in it compiles. Only a real
# `npm run build` does, and every one of those three was wrong at some point in
# the base components.
#
# Also asserts that ToastHost is actually mounted: a toast system nothing
# renders fails silently, which is the worst way for it to fail.
set -uo pipefail

JFAST=${JFAST:-jfast}
WORK=$(mktemp -d)
rc=0

cleanup() {
  saved=$?
  rm -rf "${WORK}"
  exit "${saved}"
}
trap cleanup EXIT

if ! command -v npm > /dev/null 2>&1; then
  echo "SKIP: npm is not installed"
  exit 0
fi

for framework in vue react; do
  echo "### ${framework}"
  dir="${WORK}/${framework}"
  mkdir -p "${dir}"
  cd "${dir}" || exit 1

  if ! "${JFAST}" new service web --kind spa --frontend "${framework}" --agent-docs \
       > "${dir}/gen.log" 2>&1; then
    echo "  FAIL: generate"; tail -10 "${dir}/gen.log"; rc=1; continue
  fi
  cd web || exit 1

  # The host has to be mounted somewhere, or every toast is invisible.
  if ! grep -rq "ToastHost" src/main.js src/main.jsx src/App.vue 2>/dev/null; then
    echo "  FAIL: ToastHost is never mounted"
    rc=1
    continue
  fi

  # One toast store, not two. Two means a toast raised from a service lands in
  # a list the host does not render, and nothing appears with no error.
  stores=$(grep -rlE "create\(|defineStore\(" src/stores src/hooks src/composables 2>/dev/null \
    | xargs -r grep -lE "toasts" 2>/dev/null | wc -l)
  if [ "${stores}" -gt 1 ]; then
    echo "  FAIL: ${stores} competing toast stores"
    rc=1
    continue
  fi

  echo "  installing"
  if ! npm install --silent --no-audit --no-fund > "${dir}/install.log" 2>&1; then
    echo "  FAIL: npm install"; tail -15 "${dir}/install.log"; rc=1; continue
  fi

  echo "  building"
  if ! npm run build > "${dir}/build.log" 2>&1; then
    echo "  FAIL: npm run build"
    tail -25 "${dir}/build.log"
    rc=1
    continue
  fi

  echo "  ok: ${framework} builds with the base components"
done

echo
if [ "${rc}" -eq 0 ]; then
  echo "PASS: both frontends build"
else
  echo "FAIL"
fi
exit "${rc}"
