#!/usr/bin/env bash
# `jfast dev` end to end, against real containers.
#
# Four claims, each of which was broken at some point and none of which a unit
# test can make:
#
#   1. `jfast start` produces a compose file that can actually come up. It used
#      not to: no root .env, so compose refused to interpolate the password,
#      and the command's own next-steps panel told you to run something that
#      could not work.
#   2. `jfast dev` brings the containers up and waits for them.
#   3. It applies the migration BEFORE serving, against the real database,
#      addressing it as a host process rather than as a container.
#   4. SIGTERM takes the children with it. Killing the parent and orphaning
#      uvicorn is how the next start fails on an address already in use.
#
# The API port sits outside the workspace block (start --port 9470 claims
# 9470-9479, including the database) so the two do not collide. Ports come from
# the 9400 block on purpose: WSL's relay owns 127.0.0.1:8001
# while Docker publishes on ::, so localhost:8001 reaches WSL and dies
# mid-handshake.
set -uo pipefail

JFAST=${JFAST:-jfast}
WORK=$(mktemp -d)
PORT=9491
rc=0

cleanup() {
  # Preserve the real exit status: a bare trap hands the script the trap's own.
  saved=$?
  [ -d "${WORK}" ] && (cd "${WORK}" && docker compose down -v --remove-orphans >/dev/null 2>&1)
  pkill -f "uvicorn main:app --host 127.0.0.1 --port ${PORT}" 2>/dev/null
  rm -rf "${WORK}"
  exit "${saved}"
}
trap cleanup EXIT

if ! command -v docker > /dev/null 2>&1; then
  echo "SKIP: docker is not installed"
  exit 0
fi

cd "${WORK}" || exit 1

echo "### jfast start"
"${JFAST}" start shop --port 9470 > "${WORK}/gen.log" 2>&1 || {
  echo "FAIL: start"; tail -20 "${WORK}/gen.log"; exit 1;
}

echo "### the compose file it wrote must be valid without further steps"
if ! docker compose config > /dev/null 2>&1; then
  echo "FAIL: docker compose config rejects the generated file"
  docker compose config 2>&1 | head -5
  exit 1
fi
echo "  ok"

cd shop || exit 1
"${JFAST}" new module widget --layout layered > /dev/null 2>&1

echo "### jfast dev"
setsid "${JFAST}" dev --port "${PORT}" --no-web > "${WORK}/dev.log" 2>&1 &
parent=$!

ready=0
for _ in $(seq 1 180); do
  curl -sf "http://127.0.0.1:${PORT}/health" > /dev/null 2>&1 && { ready=1; break; }
  kill -0 "${parent}" 2>/dev/null || break
  sleep 1
done

if [ "${ready}" -ne 1 ]; then
  echo "FAIL: the API never answered /health"
  tail -40 "${WORK}/dev.log"
  kill -TERM "${parent}" 2>/dev/null
  exit 1
fi
echo "  API is up"

echo "### did the migration reach the real database?"
container=$(docker ps --format '{{.Names}}' | grep -m1 'shop-database')
tables=$(docker exec "${container}" psql -U app -d app -tAc \
  "select coalesce(string_agg(tablename, ',' order by tablename), '(none)')
   from pg_tables where schemaname='public'" 2>&1 | tr -d ' ')
echo "  tables: ${tables}"
case "${tables}" in
  *alembic_version*) echo "  ok: alembic ran before serving" ;;
  *) echo "FAIL: no alembic_version table"; rc=1 ;;
esac

echo "### SIGTERM must not orphan the server"
kill -TERM "${parent}" 2>/dev/null
sleep 4
left=$(pgrep -fc "uvicorn main:app --host 127.0.0.1 --port ${PORT}" || true)
if [ "${left:-0}" -ne 0 ]; then
  echo "FAIL: ${left} orphan(s) survived"
  rc=1
else
  echo "  ok: nothing left behind"
fi

[ "${rc}" -eq 0 ] && echo "PASS"
exit "${rc}"
