#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
target=${1:?Usage: sh scripts/restore.sh BACKUP_DIRECTORY --replace-database}
test "${2:-}" = '--replace-database' || { echo 'Explicit --replace-database is required.' >&2; exit 1; }
test -s "$target/ipam.dump"
test -s "$target/app-data.tar.gz"
compose() {
  docker compose --project-name "${IPAM_PROJECT_NAME:-ipam}" --env-file "${IPAM_ENV_FILE:-.env}" -f "${IPAM_COMPOSE_FILE:-compose.yaml}" "$@"
}
compose stop app
compose exec -T postgres sh -c 'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges --exit-on-error' < "$target/ipam.dump"
compose run --rm --no-deps -T --entrypoint tar app -C /data -xzf - < "$target/app-data.tar.gz"
compose up -d --no-deps --wait app
echo 'Database and archived application files restored. Installation .env was preserved.'
