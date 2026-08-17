#!/usr/bin/env bash
# Shippable sanity check: run the full test suite against a throwaway
# pgvector/Postgres container with CPU embeddings — no host Postgres, no GPU,
# nothing persisted. This is the portable "does it work on a clean DB" gate.
#
# It uses `docker run` directly (not docker-compose) so it works regardless of
# compose version, and a distinct container/port so it never clashes with a
# host Postgres (5432) or the dev stack (5433).
#
# The test runner itself uses the local .venv (reusing the cached embedding
# model); only the DATABASE is containerized. conftest.py auto-provisions an
# empty jmfts_test in the container (jmfts is superuser there), so no manual DB
# setup is needed.
#
#   ./scripts/run_tests_docker.sh                 # run the whole suite
#   ./scripts/run_tests_docker.sh tests/test_write_races.py -q   # pass pytest args
set -euo pipefail

IMAGE="pgvector/pgvector:pg16"
CONTAINER="jmfts-ci-pg"
HOST_PORT="${JMFTS_CI_PG_PORT:-5434}"
PY="${PYTHON:-./.venv/bin/python}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup  # remove any leftover from a previous aborted run

echo ">> starting $IMAGE as $CONTAINER on 127.0.0.1:$HOST_PORT"
docker run -d --name "$CONTAINER" \
  -e POSTGRES_USER=jmfts -e POSTGRES_PASSWORD=jmfts -e POSTGRES_DB=jmfts \
  -p "127.0.0.1:${HOST_PORT}:5432" \
  "$IMAGE" >/dev/null

echo -n ">> waiting for postgres to be ready"
for _ in $(seq 1 60); do
  if docker exec "$CONTAINER" pg_isready -U jmfts -d jmfts >/dev/null 2>&1; then
    echo " — ready"
    break
  fi
  echo -n "."
  sleep 1
done
if ! docker exec "$CONTAINER" pg_isready -U jmfts -d jmfts >/dev/null 2>&1; then
  echo " — TIMED OUT" >&2
  docker logs "$CONTAINER" >&2 || true
  exit 1
fi

echo ">> running the suite (CPU embeddings, empty jmfts_test)"
# conftest.py provisions jmfts_test in the container from these JMFTS_DB_* and
# points the app at it. Force CPU so this needs no GPU.
export JMFTS_DB_HOST=localhost
export JMFTS_DB_PORT="$HOST_PORT"
export JMFTS_DB_USER=jmfts
export JMFTS_DB_PASSWORD=jmfts
export JMFTS_DB_NAME=jmfts
export JMFTS_EMBEDDING_DEVICE=cpu

if [ "$#" -gt 0 ]; then
  "$PY" -m pytest "$@"
else
  "$PY" -m pytest tests/ -q
fi
