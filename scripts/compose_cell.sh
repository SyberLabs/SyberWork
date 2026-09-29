#!/bin/bash
# Boot one cell from Compose, settle an effect with the worker, restart, then restore a backup.
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose build
docker compose up -d db --wait
docker compose run --rm -T syberwork syberwork init
set +e
output=$(docker compose run --rm -T syberwork python -m syberwork.smoke queue)
status=$?
set -e
printf '%s\n' "$output"
if [ "$status" -ne 0 ]; then
  exit "$status"
fi
case_id=$(printf '%s\n' "$output" | sed -n 's/.*CASE //p' | tail -n 1)
if [ -z "$case_id" ]; then
  echo "smoke queue did not print a case id" >&2
  exit 1
fi
docker compose up -d syberwork worker
i=0
while [ "$i" -lt 30 ]; do
  if curl -fsS http://127.0.0.1:8766/ >/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 1
done
curl -fsS http://127.0.0.1:8766/ >/dev/null
docker compose run --rm -T syberwork python -m syberwork.smoke wait "$case_id"
docker compose restart syberwork worker
i=0
while [ "$i" -lt 30 ]; do
  if curl -fsS http://127.0.0.1:8766/ >/dev/null; then
    break
  fi
  i=$((i + 1))
  sleep 1
done
curl -fsS http://127.0.0.1:8766/ >/dev/null
docker compose run --rm -T syberwork python -m syberwork.smoke verify "$case_id"
docker compose run --rm -T syberwork python -m syberwork.smoke backup-restore "$case_id"
docker compose down -v
