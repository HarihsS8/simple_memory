#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

if [ ! -f .env ]; then
    printf '%s\n' 'Run sh scripts/setup-local.sh first.' >&2
    exit 1
fi

set -a
. ./.env
set +a

command -v curl >/dev/null 2>&1 || { printf '%s\n' 'curl is required.' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'python3 is required.' >&2; exit 1; }

umask 077
backup_dir=${1:-"$project_dir/backups"}
mkdir -p "$backup_dir"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
qdrant_url="http://127.0.0.1:${QDRANT_PORT:-6333}"

docker compose exec -T postgres pg_dump -U memory_owner -d chat_memory -Fc \
    > "$backup_dir/postgres-$timestamp.dump"

snapshot=$(curl -fsS -X POST \
    "$qdrant_url/collections/$QDRANT_COLLECTION/snapshots" \
    -H "api-key: $QDRANT_API_KEY")
snapshot_name=$(printf '%s' "$snapshot" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["name"])')
case "$snapshot_name" in
    *[!A-Za-z0-9_.-]*|'') printf '%s\n' 'Qdrant returned an invalid snapshot name.' >&2; exit 1 ;;
esac

curl -fsS "$qdrant_url/collections/$QDRANT_COLLECTION/snapshots/$snapshot_name" \
    -H "api-key: $QDRANT_API_KEY" \
    -o "$backup_dir/qdrant-$timestamp.snapshot"
curl -fsS -X DELETE \
    "$qdrant_url/collections/$QDRANT_COLLECTION/snapshots/$snapshot_name" \
    -H "api-key: $QDRANT_API_KEY" >/dev/null

printf 'PostgreSQL and Qdrant backups created in %s\n' "$backup_dir"