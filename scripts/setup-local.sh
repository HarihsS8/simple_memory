#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

if ! command -v openssl >/dev/null 2>&1; then
    printf '%s\n' 'OpenSSL is required to generate local database credentials.' >&2
    exit 1
fi

if [ -e .env ]; then
    if ! grep -q '^POSTGRES_PASSWORD=' .env || ! grep -q '^MEMORY_APP_PASSWORD=' .env; then
        printf '%s\n' '.env exists but is missing PostgreSQL credentials; inspect it manually.' >&2
        exit 1
    fi

    umask 077
    if ! grep -q '^QDRANT_API_KEY=' .env; then
        printf 'QDRANT_API_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
    fi
    if ! grep -q '^QDRANT_PORT=' .env; then
        printf 'QDRANT_PORT=6333\n' >> .env
    fi
    if ! grep -q '^MEMORY_ADMIN_TOKEN=' .env; then
        printf 'MEMORY_ADMIN_TOKEN=%s\n' "$(openssl rand -hex 32)" >> .env
    fi
    if ! grep -q '^MEMORY_API_PORT=' .env; then
        printf 'MEMORY_API_PORT=8080\n' >> .env
    fi
    if ! grep -q '^QDRANT_COLLECTION=' .env; then
        printf 'QDRANT_COLLECTION=agent-memory\n' >> .env
    fi
    if ! grep -q '^VECTOR_SIZE=' .env; then
        printf 'VECTOR_SIZE=1536\n' >> .env
    fi
    if ! grep -q '^EMBEDDING_BASE_URL=' .env; then
        printf 'EMBEDDING_BASE_URL=https://api.openai.com/v1\n' >> .env
    fi
    if ! grep -q '^EMBEDDING_API_KEY=' .env; then
        printf 'EMBEDDING_API_KEY=\n' >> .env
    fi
    if ! grep -q '^EMBEDDING_MODEL=' .env; then
        printf 'EMBEDDING_MODEL=text-embedding-3-small\n' >> .env
    fi
    chmod 600 .env
    printf '%s\n' 'Existing credentials preserved; local Qdrant settings are ready.'
    exit 0
fi

umask 077
{
    printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 32)"
    printf 'MEMORY_APP_PASSWORD=%s\n' "$(openssl rand -hex 32)"
    printf 'QDRANT_API_KEY=%s\n' "$(openssl rand -hex 32)"
    printf 'POSTGRES_PORT=5432\n'
    printf 'QDRANT_PORT=6333\n'
    printf 'MEMORY_ADMIN_TOKEN=%s\n' "$(openssl rand -hex 32)"
    printf 'MEMORY_API_PORT=8080\n'
    printf 'QDRANT_COLLECTION=agent-memory\n'
    printf 'VECTOR_SIZE=1536\n'
    printf 'EMBEDDING_BASE_URL=https://api.openai.com/v1\n'
    printf 'EMBEDDING_API_KEY=\n'
    printf 'EMBEDDING_MODEL=text-embedding-3-small\n'
} > .env

printf '%s\n' 'Created .env with unique credentials and owner-only permissions.'