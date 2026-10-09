# Agent Memory Service

Tenant-scoped agent memory with authenticated HTTP APIs, PostgreSQL chat history
and compaction, and Qdrant semantic retrieval. n8n and agents call the service;
they do not receive database or Qdrant credentials.

## Run locally

Requirements: Docker Compose v2 and OpenSSL. Generate unique local credentials
and start the stack:

```sh
sh scripts/setup-local.sh
docker compose up -d --build --wait
```

On a standard Docker host, all containers use the private `chat-memory-net`
bridge. The API, PostgreSQL, and Qdrant host ports are bound to `127.0.0.1`;
data persists in named volumes. The API is at `http://localhost:8080`.

This Codespaces environment blocks container-to-container bridge traffic. For
host-run n8n in this environment, start with the host-network override instead:

```sh
docker compose -f compose.yaml -f compose.host-network.yaml up -d --build --wait
```

The override is for this restricted local environment only. For normal Docker
deployments and n8n containers, use the default bridge configuration; connect
the n8n container to `chat-memory-net` and use `http://memory-api:8080`.

The `.env` file is ignored by Git and has owner-only permissions. Keep it
private. `MEMORY_ADMIN_TOKEN` provisions tenant credentials; the generated
Qdrant key and PostgreSQL app password are used only by the API container.

## Tenant credentials

Create a tenant API key with the admin token. The raw key is returned once;
store it in n8n's credential store, not in a workflow prompt or source file.

```sh
set -a
. ./.env
set +a
curl -fsS -X POST http://localhost:8080/admin/v1/api-keys --header "X-Admin-Token: $MEMORY_ADMIN_TOKEN" --header 'content-type: application/json' --data '{"tenant_id":"acme","label":"n8n-agent"}'
```

Revoke a key with `DELETE /admin/v1/api-keys/{key_id}` and the same admin
header. API keys are stored as SHA-256 hashes. Tenant identity comes only from
the authenticated key; callers cannot select another tenant in request data.

## API and n8n

Create an n8n **Header Auth** credential with header `Authorization` and value
`Bearer <tenant-api-key>`. Use HTTP Request nodes against `http://memory-api:8080`
from Docker-connected n8n, or `http://localhost:8080` from host-run n8n.

- `POST /v1/sessions/{session_id}/messages` stores a user, assistant, system,
   or tool turn. Send JSON with `role`, `content`, optional `metadata`, and an
   `idempotency_key` for retry-safe writes.
- `GET /v1/sessions/{session_id}/context?limit=20` returns the compacted
   summary and recent ordered messages for the authenticated tenant.
- `POST /v1/sessions/{session_id}/compact` accepts a complete replacement
   `summary` and `through_message_id`; it atomically deletes only summarized
   messages.
- `PUT /v1/memories` upserts semantic memory. Supply `text`, optional
   `metadata`/`session_id`, and either an embedding `vector` or configure the
   embedding provider below.
- `POST /v1/memories/search` accepts `query` text or a `vector`, optional
   `session_id`, and `limit`; every query is filtered to the authenticated
   tenant before reaching Qdrant.
- `DELETE /v1/memories/{memory_id}` and `DELETE /v1/sessions/{session_id}`
   remove tenant-scoped vector and conversation data.

PostgreSQL row-level security also scopes session operations using a
transaction-local tenant identity. Message appends are idempotent when a key is
provided and serialize against compaction. Qdrant payload filters are built by
the service; user-supplied filters are not accepted.

## Schema and upgrades

Fresh databases apply the schema and grants during initialization. Init scripts
run only when PostgreSQL creates its data volume. To upgrade an existing volume,
apply the tenant schema and grants as the owner:

```sh
docker compose exec -T postgres psql -U memory_owner -d chat_memory -v ON_ERROR_STOP=1 -f - < agent_memory.sql
docker compose exec -T postgres psql -U memory_owner -d chat_memory -v ON_ERROR_STOP=1 -f - < docker/grants.sql
```

The `memory_app` role is now used only by the API service; its old direct
`chat_memory` function grants are revoked. Keep `.env` during database
migrations because the app password is not stored in PostgreSQL.
Legacy `chat_memory` sessions are not automatically imported: they have no
tenant ownership. Assign each legacy session to its rightful tenant before
migrating it; do not bulk-assign potentially shared history to one customer.

## Embeddings and RAG

Set `EMBEDDING_API_KEY` in `.env` to let the service create embeddings through
the configured OpenAI-compatible endpoint. Defaults are OpenAI's
`text-embedding-3-small` and 1536 dimensions. For n8n-managed or local embedding
models, send their vector in the `vector` field instead. The vector length must
match `VECTOR_SIZE`; use the same model and dimensions for writes and searches.
Use a new collection and re-index when changing embedding models.

In n8n, load/split source documents, embed each chunk, and `PUT /v1/memories`.
For a user query, call `POST /v1/memories/search`, then pass the returned text
and scores as retrieved context to the agent. Keep source/access metadata and
scope each request to a tenant API key. PostgreSQL compaction does not delete
semantic memories unless the session-delete endpoint is called.

## Operations

Run the end-to-end API test. It creates disposable tenant keys, exercises
cross-tenant isolation, and revokes those keys afterward:

```sh
python3 tests/api-smoke.py
```

The legacy SQL schema smoke test can be run as the database owner:

```sh
docker compose exec -T postgres psql -U memory_owner -d chat_memory -v ON_ERROR_STOP=1 -f - < tests/smoke.sql
```

Verify the authenticated local Qdrant RAG API path (requires `curl`):

```sh
sh tests/qdrant-smoke.sh
```

Create a PostgreSQL dump and Qdrant snapshot together:

```sh
sh scripts/backup-local.sh
```

The files are saved under `backups/`. Restore PostgreSQL objects with:

```sh
docker compose exec -T postgres pg_restore -U memory_owner -d chat_memory --clean --if-exists < backups/postgres-FILE.dump
```

Restore Qdrant snapshots through the Qdrant snapshot recovery API. Store both
backups encrypted and off-machine, and periodically test restoring both stores.
The Compose setup uses single local PostgreSQL and Qdrant instances, not a
highly available production cluster. For internet-facing or multi-host
production, use managed services or provide TLS, network restrictions, secret
management, monitoring, vector-store backup/rebuild procedures, and a tested
recovery plan; do not expose these loopback-only services directly.
