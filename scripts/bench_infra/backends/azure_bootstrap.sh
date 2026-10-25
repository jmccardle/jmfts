#!/bin/bash
# bench_infra host bootstrap, delivered as Azure custom data and run once by cloud-init.
# Rendered by backends/azure.py, which fills every placeholder (a NAME between double @) first.
#
# It does as little as it can in cloud-init itself: write two scripts, start them as
# transient systemd units, return. `bench-runner` installs what the workload's host
# requirements name and runs the shard runner; `bench-deadline` fires at the job's deadline
# whatever state the runner is in. Both end in finish.sh, which uploads the logs and then
# deletes this VM (with the managed identity, if one was attached) or powers it off.
set -uo pipefail

mkdir -p /opt/bench
cat > /opt/bench/env.sh <<'ENVEOF'
export BENCH_STORE='@@STORE@@'
export BENCH_JOB='@@JOB@@'
export BENCH_HOST='@@HOST@@'
export BENCH_IDENTITY_CLIENT_ID='@@IDENTITY_CLIENT_ID@@'
export BENCH_NEEDS_POSTGRES='@@NEEDS_POSTGRES@@'
export BENCH_NEEDS_LIBREOFFICE='@@NEEDS_LIBREOFFICE@@'
export BENCH_EXTRAS='@@EXTRAS@@'
export BENCH_TORCH='@@TORCH@@'
export BENCH_PG_IMAGE='@@PG_IMAGE@@'
ENVEOF

cat > /opt/bench/put.sh <<'PUTEOF'
#!/bin/bash
# put.sh KEY FILE — upload FILE to the job's store under jobs/$BENCH_JOB/KEY.
. /opt/bench/env.sh
base="${BENCH_STORE%%\?*}"; sas="${BENCH_STORE#*\?}"
curl -sS --retry 4 -X PUT -H "x-ms-blob-type: BlockBlob" -H "x-ms-version: 2021-08-06" \
  --data-binary "@$2" "$base/jobs/$BENCH_JOB/$1?$sas" >/dev/null
PUTEOF

cat > /opt/bench/finish.sh <<'FINEOF'
#!/bin/bash
# finish.sh REASON — upload what we have, then delete this VM or power it off.
. /opt/bench/env.sh
echo "$(date -u +%FT%TZ) finish: $1" >> /var/log/bench-bootstrap.log
/opt/bench/put.sh "hosts/$BENCH_HOST.bootstrap.log" /var/log/bench-bootstrap.log
[ -f /var/log/bench-runner.log ] && /opt/bench/put.sh "hosts/$BENCH_HOST.runner.log" /var/log/bench-runner.log
if [ -n "$BENCH_IDENTITY_CLIENT_ID" ]; then
  token=$(curl -sS -H Metadata:true "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/&client_id=$BENCH_IDENTITY_CLIENT_ID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
  rid=$(curl -sS -H Metadata:true "http://169.254.169.254/metadata/instance/compute/resourceId?api-version=2021-02-01&format=text")
  if [ -n "$token" ] && [ -n "$rid" ]; then
    # The VM was created with deleteOption=Delete on its disk, NIC and public IP, so
    # deleting the VM removes all four.
    curl -sS -X DELETE -H "Authorization: Bearer $token" \
      "https://management.azure.com${rid}?api-version=2024-07-01&forceDeletion=true" && exit 0
  fi
fi
# No identity, or the delete failed: stop burning CPU. A stopped VM is still billed until
# `python -m scripts.bench_infra sweep` deletes it.
poweroff
FINEOF

cat > /opt/bench/run.sh <<'RUNEOF'
#!/bin/bash
set -uo pipefail
. /opt/bench/env.sh
exec >> /var/log/bench-bootstrap.log 2>&1
t0=$(date +%s)
stamp() { echo "$(date -u +%FT%TZ) [+$(( $(date +%s) - t0 ))s] $*"; }
fail() { stamp "FAILED: $*"; /opt/bench/finish.sh "bootstrap failed: $*"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

stamp "apt"
pkgs="python3-venv python3-pip git curl"
if [ "$BENCH_NEEDS_POSTGRES" = 1 ] || [ "$BENCH_NEEDS_LIBREOFFICE" = 1 ]; then pkgs="$pkgs docker.io"; fi
# tests/conftest.py provisions its test database with the `psql` CLI, not a driver.
if [ "$BENCH_NEEDS_POSTGRES" = 1 ]; then pkgs="$pkgs postgresql-client"; fi
for i in 1 2 3; do apt-get update -q && apt-get install -y -q $pkgs && break; sleep 20; done || fail apt
t_apt=$(( $(date +%s) - t0 ))

stamp "source"
mkdir -p /opt/bench/src && cd /opt/bench/src
base="${BENCH_STORE%%\?*}"; sas="${BENCH_STORE#*\?}"
curl -sSf --retry 4 "$base/jobs/$BENCH_JOB/src.tar.gz?$sas" | tar xz || fail "source download"

PY=python3
if [ -n "$BENCH_EXTRAS" ]; then
  stamp "venv ($BENCH_EXTRAS)"
  python3 -m venv /opt/bench/venv && PY=/opt/bench/venv/bin/python
  $PY -m pip install -q --upgrade pip || fail pip
  if [ "$BENCH_TORCH" = 1 ]; then
    $PY -m pip install -q --index-url https://download.pytorch.org/whl/cpu torch || fail torch
  fi
  $PY -m pip install -q -e ./jmfts-client || fail jmfts-client
  $PY -m pip install -q -e ".[$BENCH_EXTRAS]" || fail "jmfts[$BENCH_EXTRAS]"
fi
t_pip=$(( $(date +%s) - t0 ))

if [ "$BENCH_NEEDS_POSTGRES" = 1 ]; then
  stamp "postgres $BENCH_PG_IMAGE"
  docker run -d --name pg --restart always -p 127.0.0.1:5432:5432 \
    -e POSTGRES_USER=jmfts -e POSTGRES_PASSWORD=jmfts -e POSTGRES_DB=jmfts "$BENCH_PG_IMAGE" || fail postgres
  for i in $(seq 1 60); do docker exec pg pg_isready -U jmfts >/dev/null 2>&1 && break; sleep 2; done
  export JMFTS_DB_HOST=127.0.0.1 JMFTS_DB_PORT=5432 JMFTS_DB_USER=jmfts JMFTS_DB_PASSWORD=jmfts JMFTS_DB_NAME=jmfts
fi
if [ "$BENCH_NEEDS_LIBREOFFICE" = 1 ]; then
  stamp "libreoffice image"
  docker build -q -f Dockerfile.libreoffice -t jmfts-libreoffice . || fail "libreoffice build"
  docker run -d --name lo --restart always -p 127.0.0.1:2003:2003 jmfts-libreoffice || fail libreoffice
  export JMFTS_CONVERT_URL=http://127.0.0.1:2003
  sleep 10
fi
t_services=$(( $(date +%s) - t0 ))
export JMFTS_API_TOKEN=bench-infra

stamp "runner"
cat > /opt/bench/bootstrap.json <<EOF
{"apt_s": $t_apt, "pip_s": $t_pip, "services_s": $t_services, "ready_s": $(( $(date +%s) - t0 ))}
EOF
/opt/bench/put.sh "hosts/$BENCH_HOST.bootstrap.json" /opt/bench/bootstrap.json
/opt/bench/put.sh "hosts/$BENCH_HOST.bootstrap.log" /var/log/bench-bootstrap.log
$PY -m scripts.bench_infra.runner --store "$BENCH_STORE" --job "$BENCH_JOB" \
  --host "$BENCH_HOST" --workdir /opt/bench/work >> /var/log/bench-runner.log 2>&1
stamp "runner exited $?"
/opt/bench/finish.sh "runner finished"
RUNEOF

chmod +x /opt/bench/*.sh
systemd-run --unit=bench-deadline --on-active=@@DEADLINE_SECONDS@@ /opt/bench/finish.sh deadline
systemd-run --unit=bench-runner /opt/bench/run.sh
