#!/bin/sh
set -eu

: "${MEMORY_APP_PASSWORD:?MEMORY_APP_PASSWORD must be set}"

psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --set=ON_ERROR_STOP=1 \
    --set=app_password="$MEMORY_APP_PASSWORD" <<'SQL'
CREATE ROLE memory_app
    LOGIN
    PASSWORD :'app_password'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT;
SQL