#!/usr/bin/env bash
# Contracts, end to end.
#
#     bash scripts/smoke_contracts.sh
#
# The load-bearing assertion is that a *freshly generated* service passes its
# own contract. A contract the generator itself violates teaches everyone to
# ignore the checker on day one.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/jfast" ]]; then
  JFAST="${ROOT}/.venv/bin/jfast"
else
  JFAST="$(command -v jfast)"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
cd "${WORK}"

step() { printf '\n=== %s ===\n' "$1"; }
fail() { echo "FAIL: $1"; exit 1; }

step "a generated service ships a contract"
"${JFAST}" new service billing --with database > /dev/null
cd billing
test -f contracts.toml || fail "no contracts.toml"

step "and passes it out of the box"
"${JFAST}" new module invoice > /dev/null
"${JFAST}" contracts check

step "a layer violation is caught"
cat > modules/invoice/repository.py <<'PY'
from fastapi import APIRouter

from jfastframework.db import BaseRepository

from .models import Invoice


class InvoiceRepository(BaseRepository[Invoice]):
    model = Invoice
PY
if "${JFAST}" contracts check > /tmp/contracts.txt 2>&1; then
  fail "a storage layer importing fastapi should not pass"
fi
grep -q 'layer-package' /tmp/contracts.txt || fail "wrong rule fired: $(cat /tmp/contracts.txt)"
echo "caught: $(grep layer-package /tmp/contracts.txt | head -1)"

step "a waiver clears it, and is listed"
sed -i 's|^from fastapi import APIRouter$|from fastapi import APIRouter  # contracts: allow spike, JF-1|' \
  modules/invoice/repository.py
"${JFAST}" contracts check
"${JFAST}" contracts waivers | grep -q 'spike, JF-1' || fail "waiver not listed"

step "a forbidden call is caught"
git checkout modules/invoice/repository.py 2> /dev/null || true
cat > modules/invoice/service.py <<'PY'
import os


class InvoiceService:
    def __init__(self, repository: object) -> None:
        self.repository = repository
        self.mode = os.getenv("MODE")
PY
if "${JFAST}" contracts check > /tmp/contracts2.txt 2>&1; then
  fail "os.getenv outside settings should not pass"
fi
grep -q 'forbid-call' /tmp/contracts2.txt || fail "wrong rule: $(cat /tmp/contracts2.txt)"
grep -q 'Configuration is typed' /tmp/contracts2.txt || fail "the reason was not shown"

step "the contract is machine-readable"
"${JFAST}" contracts show --json | grep -q '"does_not_own"' || fail "no does_not_own in JSON"
# `check` exits non-zero by design, and pipefail would make that the
# pipeline's status. Capture first, then inspect.
CHECK_JSON="$("${JFAST}" contracts check --json || true)"
grep -q '"violations"' <<< "${CHECK_JSON}" || fail "no violations key"
grep -q '"ok": false' <<< "${CHECK_JSON}" || fail "ok should be false here"

step "CONTRACTS.md renders"
"${JFAST}" contracts render
grep -q 'May import' CONTRACTS.md || fail "no layer table"
grep -q 'contracts: allow' CONTRACTS.md || fail "no waiver instructions"

step "the screaming layout gets its own defaults"
cd "${WORK}"
"${JFAST}" new service catalog --with database > /dev/null
cd catalog
"${JFAST}" contracts init --layout screaming --force > /dev/null
"${JFAST}" new module product --layout screaming > /dev/null
"${JFAST}" contracts check

printf '\nCONTRACTS SMOKE OK\n'
