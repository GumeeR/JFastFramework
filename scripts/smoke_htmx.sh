#!/usr/bin/env bash
# The HTMX form must actually submit.
#
# `--ui htmx` generated a POST handler that passed a dict to a service which
# read `payload.name` off it. Every layout does. So the one path a browser
# takes -- typing in the form and pressing the button -- raised AttributeError,
# and nothing caught it: the module rendered, imported, mounted, and passed its
# contract. Only sending the form finds this.
#
# Runs against SQLite so it needs no containers, and covers every layout,
# because the overlay is layout-agnostic and so was the bug.
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
  echo "### ${layout} --ui htmx"
  dir="${WORK}/${layout}"
  mkdir -p "${dir}"
  cd "${dir}" || exit 1

  if ! "${JFAST}" new service shop --with database,web > "${dir}/gen.log" 2>&1; then
    echo "  FAIL: new service"; tail -10 "${dir}/gen.log"; rc=1; continue
  fi
  cd shop || exit 1

  if ! "${JFAST}" new module widget --layout "${layout}" --ui htmx > "${dir}/mod.log" 2>&1; then
    echo "  FAIL: new module"; tail -15 "${dir}/mod.log"; rc=1; continue
  fi

  if ! PYTHONPATH="${PWD}" \
       DB_FILE="${dir}/test.db" \
       JFAST_DB_DSN="sqlite+aiosqlite:///${dir}/test.db" \
       "${PY}" - > "${dir}/post.log" 2>&1 <<'PYTEST'
import os

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

# Importing main pulls in the module package, which registers its table on the
# shared metadata. Without that import the create_all below writes nothing.
import main
from jfastframework.db import Base

# A synchronous engine on the same file, purely to create the schema. Doing it
# through the app's async engine means borrowing its event loop from outside a
# request, which is more machinery than a smoke test should own.
Base.metadata.create_all(create_engine(f"sqlite:///{os.environ['DB_FILE']}"))

with TestClient(main.app) as client:
    page = client.get("/ui/widgets")
    assert page.status_code == 200, (page.status_code, page.text[:300])

    # The form submission. This is the line the bug was on.
    posted = client.post("/ui/widgets", data={"name": "first", "description": "from the form"})
    assert posted.status_code == 201, (posted.status_code, posted.text[:500])
    assert "first" in posted.text, posted.text[:500]

    # And it is really in the list afterwards, not just echoed back.
    listed = client.get("/ui/widgets")
    assert "first" in listed.text, listed.text[:500]
    print("  form POST ->", posted.status_code, "and the row is in the list")
PYTEST
  then
    echo "  FAIL: the HTMX form does not submit"
    tail -20 "${dir}/post.log"
    rc=1
    continue
  fi
  grep -a "form POST" "${dir}/post.log"
done

echo
if [ "${rc}" -eq 0 ]; then
  echo "PASS: the HTMX form submits on every layout"
else
  echo "FAIL"
fi
exit "${rc}"
