#!/usr/bin/env bash
# Every module layout must produce a service that actually imports and passes
# its own contract -- and rejects the violation that contract exists to reject.
#
# A layout is not "done" when the files render. A generated module gets spliced
# into main.py, so a missing export or a wrong import path does not fail at
# generation time -- it fails at boot, which looks like a framework bug rather
# than a template one.
#
# This runs the sequence a user runs: `jfast new service`, then `jfast new
# module --layout X`. It used to run `jfast contracts init --layout X` instead,
# which is the path that always worked -- and that is why nobody noticed the
# scaffold writing the layered contract into all four services. Three layouts
# enforced nothing and reported a pass.
#
# Five things per layout:
#   1. `jfast new module --layout X` renders it
#   2. the contract that arrives with it is X's, not a default
#   3. main.py imports, with the module mounted and its routes registered
#   4. `jfast contracts check` passes on the code the generator just wrote
#   5. `import sqlalchemy` in the file that answers HTTP fails that check
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

# The file that answers HTTP, and a glob only this layout's contract declares.
# Both differ per layout, which is the reason the contract has to follow the
# layout rather than be picked once and reused.
http_file() {
  case "$1" in
    layered)   echo "modules/widget/router.py" ;;
    modular)   echo "modules/widget/api/routes.py" ;;
    screaming) echo "modules/widget/http.py" ;;
    hexagonal) echo "modules/widget/adapters/http.py" ;;
  esac
}

contract_glob() {
  case "$1" in
    layered)   echo 'modules/*/router.py' ;;
    modular)   echo 'modules/*/api/*.py' ;;
    screaming) echo 'modules/*/use_cases/*.py' ;;
    hexagonal) echo 'modules/*/adapters/*.py' ;;
  esac
}

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

  # The contract arrives with the first module, because that is the first
  # moment a layout exists to read.
  if [ ! -f contracts.toml ]; then
    echo "  FAIL: new module wrote no contracts.toml"; rc=1; continue
  fi
  if ! grep -qF "$(contract_glob "${layout}")" contracts.toml; then
    echo "  FAIL: contracts.toml is not the ${layout} contract"
    grep -n '^paths' contracts.toml
    rc=1
    continue
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

  # The half that can break. A contract written for another layout matches none
  # of these files, so every layer rule on it is inert -- and inert is
  # indistinguishable from clean unless something is planted.
  target=$(http_file "${layout}")
  cp "${target}" "${dir}/http.bak"
  printf 'import sqlalchemy\n' | cat - "${dir}/http.bak" > "${target}"
  planted_rc=0
  "${JFAST}" contracts check > "${dir}/planted.log" 2>&1 || planted_rc=$?
  cp "${dir}/http.bak" "${target}"
  if [ "${planted_rc}" -eq 0 ]; then
    echo "  FAIL: sqlalchemy in ${target} passed the ${layout} contract"; rc=1; continue
  fi
  if ! grep -q 'layer-package' "${dir}/planted.log"; then
    echo "  FAIL: wrong rule fired for ${target}"; head -5 "${dir}/planted.log"; rc=1; continue
  fi

  echo "  ok: renders, imports, mounts, passes its contract, rejects the ORM in ${target}"
done

echo
if [ "${rc}" -eq 0 ]; then
  echo "PASS: all four layouts"
else
  echo "FAIL"
fi
exit "${rc}"
