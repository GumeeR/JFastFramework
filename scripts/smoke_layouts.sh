#!/usr/bin/env bash
# Every module layout must produce a service that actually imports and passes
# its own contract.
#
# A layout is not "done" when the files render. A generated module gets spliced
# into main.py, so a missing export or a wrong import path does not fail at
# generation time -- it fails at boot, which looks like a framework bug rather
# than a template one.
#
# Four things per layout:
#   1. `jfast new module` renders it
#   2. `jfast contracts init --layout X` finds a contract template
#   3. main.py imports, with the module mounted and its routes registered
#   4. `jfast contracts check` passes on the code the generator just wrote
set -uo pipefail

JFAST=${JFAST:-jfast}
PY=${PY:-python3}
WORK=$(mktemp -d)
rc=0

cleanup() {
  saved=$?
  rm -rf "${WORK}"
  exit "${saved}"
}
trap cleanup EXIT

for layout in layered modular screaming hexagonal; do
  echo "### ${layout}"
  dir="${WORK}/${layout}"
  mkdir -p "${dir}"
  cd "${dir}" || exit 1

  if ! "${JFAST}" new service shop --with database > "${dir}/gen.log" 2>&1; then
    echo "  FAIL: new service"; tail -10 "${dir}/gen.log"; rc=1; continue
  fi
  cd shop || exit 1

  if ! "${JFAST}" new module widget --layout "${layout}" > "${dir}/mod.log" 2>&1; then
    echo "  FAIL: new module --layout ${layout}"; tail -15 "${dir}/mod.log"; rc=1; continue
  fi

  if ! "${JFAST}" contracts init --layout "${layout}" --force > "${dir}/con.log" 2>&1; then
    echo "  FAIL: contracts init --layout ${layout}"; tail -10 "${dir}/con.log"; rc=1; continue
  fi

  # Does the service import with the module mounted? Nothing else proves the
  # generated package exports what main.py was told it exports.
  if ! PYTHONPATH="${PWD}" JFAST_DB_DSN="postgresql+asyncpg://x:x@localhost/x" \
       "${PY}" -c "
import main
# The OpenAPI document, not app.routes: an included router appears there as a
# _IncludedRouter with no .path, so walking app.routes finds neither the
# module's endpoints nor /health.
paths = sorted(main.app.openapi()['paths'])
matched = [p for p in paths if 'widget' in p]
assert matched, f'no widget routes among {paths}'
print('  routes:', matched)
" 2>"${dir}/import.log"; then
    echo "  FAIL: main.py does not import with the module mounted"
    tail -15 "${dir}/import.log"
    rc=1
    continue
  fi

  if ! "${JFAST}" contracts check > "${dir}/check.log" 2>&1; then
    echo "  FAIL: the generated code violates its own contract"
    head -15 "${dir}/check.log"
    rc=1
    continue
  fi

  echo "  ok: renders, imports, mounts, and passes its contract"
done

echo
if [ "${rc}" -eq 0 ]; then
  echo "PASS: all four layouts"
else
  echo "FAIL"
fi
exit "${rc}"
