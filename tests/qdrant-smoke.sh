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

api_url="http://127.0.0.1:${QDRANT_PORT:-6333}"
collection="smoke_$(date +%s)_$$"
auth_header="api-key: ${QDRANT_API_KEY}"

cleanup() {
    curl -fsS -X DELETE "$api_url/collections/$collection" -H "$auth_header" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

curl -fsS "$api_url/readyz" -H "$auth_header" >/dev/null

if curl -fsS "$api_url/collections" >/dev/null 2>&1; then
    printf '%s\n' 'Qdrant accepted a request without an API key.' >&2
    exit 1
fi

curl -fsS -X PUT "$api_url/collections/$collection" \
    -H "$auth_header" -H 'content-type: application/json' \
    -d '{"vectors":{"size":3,"distance":"Cosine"}}' >/dev/null

curl -fsS -X PUT "$api_url/collections/$collection/points?wait=true" \
    -H "$auth_header" -H 'content-type: application/json' \
    -d '{"points":[{"id":1,"vector":[0.1,0.2,0.3],"payload":{"session_id":"smoke"}}]}' >/dev/null

result=$(curl -fsS -X POST "$api_url/collections/$collection/points/search" \
    -H "$auth_header" -H 'content-type: application/json' \
    -d '{"vector":[0.1,0.2,0.3],"limit":1,"with_payload":true}')

printf '%s' "$result" | grep -q '"id":1'
printf '%s\n' 'Qdrant API key, collection, upsert, and vector search passed.'