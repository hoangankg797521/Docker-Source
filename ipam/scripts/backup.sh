#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
target=${1:-backups/$(date -u +%Y%m%dT%H%M%SZ)}
test ! -e "$target" || { echo 'Backup target exists; preserved.' >&2; exit 1; }
umask 077
mkdir -p "$target"
compose() {
  docker compose --project-name "${IPAM_PROJECT_NAME:-ipam}" --env-file "${IPAM_ENV_FILE:-.env}" -f "${IPAM_COMPOSE_FILE:-compose.yaml}" "$@"
}
compose exec -T postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges' > "$target/ipam.dump"
compose exec -T app tar -C /data -czf - . > "$target/app-data.tar.gz"
cp "${IPAM_ENV_FILE:-.env}" "$target/installation.env"
compose images -q app > "$target/image-id.txt"
printf '%s\n' "Backup written: $target (contains private site data and credentials)."
