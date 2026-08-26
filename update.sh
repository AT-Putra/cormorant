#!/usr/bin/env bash
# Server-side update: fetch compose/env changes, pull the image CI built, swap
# the container. Named volumes are untouched by recreation, so the SQLite DB,
# the Fernet key and the yt-dlp venv updated from the UI all survive this.
#
#   ./update.sh              # move to whatever :latest now points at
#   TAG=<git-sha> ./update.sh  # pin/roll back to one build
set -euo pipefail
cd "$(dirname "$0")"

git pull --ff-only
docker compose pull
docker compose up -d

# Don't report success on a container that came up and fell over: the app's own
# HEALTHCHECK is the source of truth, so wait for it rather than for `up` to
# return. start-period is 15s, interval 30s — 90s covers a first-run migration.
printf 'waiting for health'
for _ in $(seq 1 45); do
  status=$(docker inspect --format '{{.State.Health.Status}}' cormorant 2>/dev/null || echo missing)
  case "$status" in
    healthy)   echo " -> healthy"; docker compose ps; exit 0 ;;
    unhealthy) echo " -> unhealthy"; docker compose logs --tail 40 app; exit 1 ;;
  esac
  printf '.'; sleep 2
done

echo " -> timed out after 90s (status: ${status})"
docker compose logs --tail 40 app
exit 1
