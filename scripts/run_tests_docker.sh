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
HOST_PORT="${JMFTS_CI_PG_PORT:-5434}"

# The container name carries the port, and that is not cosmetic. `cleanup` below runs
# `docker rm -f "$CONTAINER"` at STARTUP as well as on exit, to clear a leftover from an
# aborted run. Under a fixed name, a second run starting while a first is still going
# deletes the first one's database out from under it — the suite then fails partway
# through with connection errors that look like flakes and are not.
#
# Two runs in parallel is not hypothetical: it is what several worktrees, or several
# agents, do. One variable now moves both the port and the name, so concurrent runs need
# only `JMFTS_CI_PG_PORT` to be distinct.
CONTAINER="jmfts-ci-pg-${HOST_PORT}"
PY="${PYTHON:-./.venv/bin/python}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# `-v` matters. The pgvector image declares VOLUME /var/lib/postgresql/data, so every
# `docker run` here creates an anonymous volume, and `docker rm` without `-v` removes the
# container and leaves the volume behind — about 128 MB of abandoned PGDATA per suite run.
cleanup() { docker rm -f -v "$CONTAINER" >/dev/null 2>&1 || true; }
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
