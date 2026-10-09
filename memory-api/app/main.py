import asyncio
import hashlib
import json
import logging
import re
import math
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any

import asyncpg
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.models import AppendMessage, CompactSession, CreateApiKey, MemorySearch, MemoryWrite


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("agent_memory")

DATABASE_URL = os.environ["DATABASE_URL"]
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
QDRANT_API_KEY = os.environ["QDRANT_API_KEY"]
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "agent-memory")
VECTOR_SIZE = int(os.getenv("VECTOR_SIZE", "1536"))
ADMIN_TOKEN = os.environ["MEMORY_ADMIN_TOKEN"]
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1").rstrip("/")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
MAX_METADATA_BYTES = 65_536

if len(ADMIN_TOKEN) < 32:
    raise RuntimeError("MEMORY_ADMIN_TOKEN must contain at least 32 characters")
if not 1 <= VECTOR_SIZE <= 8192:
    raise RuntimeError("VECTOR_SIZE must be between 1 and 8192")

bearer = HTTPBearer(auto_error=False)


async def init_connection(connection: asyncpg.Connection) -> None:
    await connection.set_type_codec(
        "jsonb", schema="pg_catalog", encoder=json.dumps, decoder=json.loads, format="text"
    )


async def verify_collection(client: httpx.AsyncClient) -> None:
    for attempt in range(40):
        try:
            ready = await client.get("/readyz")
            ready.raise_for_status()
            break
        except (httpx.HTTPError, httpx.HTTPStatusError):
            if attempt == 39:
                raise RuntimeError("Qdrant did not become ready")
            await asyncio.sleep(0.5)

    response = await client.get(f"/collections/{QDRANT_COLLECTION}")
    if response.status_code == 404:
        created = await client.put(
            f"/collections/{QDRANT_COLLECTION}",
            json={"vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}},
        )
        if created.status_code not in (200, 409):
            created.raise_for_status()
        response = await client.get(f"/collections/{QDRANT_COLLECTION}")
    response.raise_for_status()
    vector_config = response.json()["result"]["config"]["params"]["vectors"]
    if not isinstance(vector_config, dict) or vector_config.get("size") != VECTOR_SIZE:
        raise RuntimeError(
            f"Qdrant collection {QDRANT_COLLECTION!r} must use {VECTOR_SIZE}-dimensional unnamed vectors"
        )
    for field_name in ("tenant_id", "session_id"):
        index_response = await client.put(
            f"/collections/{QDRANT_COLLECTION}/index",
            json={"field_name": field_name, "field_schema": "keyword"},
        )
        if index_response.status_code != 409:
            index_response.raise_for_status()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=2, max_size=10, command_timeout=15, init=init_connection
    )
    app.state.qdrant = httpx.AsyncClient(
        base_url=QDRANT_URL,
        headers={"api-key": QDRANT_API_KEY},
        timeout=httpx.Timeout(10.0, connect=3.0),
    )
    app.state.embedding = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=3.0))
    try:
        await app.state.pool.fetchval("SELECT 1")
        await verify_collection(app.state.qdrant)
        yield
    finally:
        await app.state.embedding.aclose()
        await app.state.qdrant.aclose()
        await app.state.pool.close()


app = FastAPI(
    title="Agent Memory API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_header(request: Request, call_next):
    request_id = request.headers.get("x-request-id", "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", request_id):
        request_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id[:128]
    return response


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(request: Request) -> dict[str, str]:
    try:
        await request.app.state.pool.fetchval("SELECT 1")
        response = await request.app.state.qdrant.get("/readyz")
        response.raise_for_status()
    except (asyncpg.PostgresError, httpx.HTTPError) as error:
        raise HTTPException(status_code=503, detail="memory dependencies are unavailable") from error
    return {"status": "ready"}


async def require_admin(
    x_admin_token: Annotated[str | None, Header()] = None,
) -> None:
    if x_admin_token is None or not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid admin credential")


async def authenticated_tenant(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="bearer token required", headers={"WWW-Authenticate": "Bearer"})
    if len(credentials.credentials) > 256:
        raise HTTPException(status_code=401, detail="invalid API credential")

    token_hash = hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()
    tenant_id = await request.app.state.pool.fetchval(
        "SELECT tenant_id FROM agent_memory.api_keys WHERE token_hash = $1 AND revoked_at IS NULL",
        token_hash,
    )
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="invalid API credential", headers={"WWW-Authenticate": "Bearer"})
    return tenant_id


@asynccontextmanager
async def tenant_connection(request: Request, tenant_id: str):
    async with request.app.state.pool.acquire() as connection:
        async with connection.transaction():
            await connection.fetchval(
                "SELECT set_config('agent_memory.tenant_id', $1, true)", tenant_id
            )
            yield connection


async def qdrant_request(request: Request, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = await request.app.state.qdrant.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}
    except httpx.HTTPError as error:
        logger.warning("Qdrant request failed: method=%s path=%s", method, path)
        raise HTTPException(status_code=503, detail="vector store is unavailable") from error


def validate_metadata(metadata: dict[str, Any]) -> None:
    if len(json.dumps(metadata, separators=(",", ":")).encode("utf-8")) > MAX_METADATA_BYTES:
        raise HTTPException(status_code=413, detail="metadata exceeds 64 KiB")


def validate_session_id(session_id: str) -> None:
    if not session_id.strip() or len(session_id.encode("utf-8")) > 512:
        raise HTTPException(status_code=422, detail="session_id must be non-empty and at most 512 bytes")


def validate_vector(vector: list[float]) -> list[float]:
    if len(vector) != VECTOR_SIZE or any(not math.isfinite(value) for value in vector):
        raise HTTPException(status_code=422, detail=f"vector must contain {VECTOR_SIZE} finite values")
    return vector


async def embed_text(request: Request, text: str) -> list[float]:
    if not EMBEDDING_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="configure EMBEDDING_API_KEY or provide an embedding vector",
        )
    try:
        response = await request.app.state.embedding.post(
            f"{EMBEDDING_BASE_URL}/embeddings",
            headers={"Authorization": f"Bearer {EMBEDDING_API_KEY}"},
            json={"model": EMBEDDING_MODEL, "input": text},
        )
        response.raise_for_status()
        vector = response.json()["data"][0]["embedding"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
        logger.warning("embedding provider request failed")
        raise HTTPException(status_code=502, detail="embedding provider request failed") from error
    return validate_vector(vector)


def tenant_filter(tenant_id: str, session_id: str | None = None) -> dict[str, Any]:
    must: list[dict[str, Any]] = [
        {"key": "tenant_id", "match": {"value": tenant_id}},
    ]
    if session_id is not None:
        must.append({"key": "session_id", "match": {"value": session_id}})
    return {"must": must}


@app.post("/admin/v1/api-keys", status_code=201)
async def create_api_key(
    body: CreateApiKey,
    request: Request,
    _: Annotated[None, Security(require_admin)],
) -> dict[str, str]:
    tenant_id = body.tenant_id.strip()
    label = body.label.strip()
    if not tenant_id or not label or len(tenant_id.encode("utf-8")) > 128 or len(label.encode("utf-8")) > 128:
        raise HTTPException(status_code=422, detail="tenant_id and label must not be blank")

    raw_key = f"am_{secrets.token_urlsafe(32)}"
    key_id = uuid.uuid4()
    token_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    async with request.app.state.pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "INSERT INTO agent_memory.tenants (tenant_id) VALUES ($1) ON CONFLICT DO NOTHING",
                tenant_id,
            )
            await connection.execute(
                "INSERT INTO agent_memory.api_keys (key_id, tenant_id, token_hash, label) VALUES ($1, $2, $3, $4)",
                key_id,
                tenant_id,
                token_hash,
                label,
            )
    return {"key_id": str(key_id), "tenant_id": tenant_id, "label": label, "api_key": raw_key}


@app.delete("/admin/v1/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: uuid.UUID,
    request: Request,
    _: Annotated[None, Security(require_admin)],
) -> Response:
    result = await request.app.state.pool.execute(
        "UPDATE agent_memory.api_keys SET revoked_at = now() WHERE key_id = $1 AND revoked_at IS NULL",
        key_id,
    )
    if result.endswith(" 0"):
        raise HTTPException(status_code=404, detail="API key not found or already revoked")
    return Response(status_code=204)


@app.post("/v1/sessions/{session_id}/messages", status_code=201)
async def append_message(
    session_id: str,
    body: AppendMessage,
    request: Request,
    tenant_id: str = Depends(authenticated_tenant),
) -> dict[str, Any]:
    validate_session_id(session_id)
    if len(body.content.encode("utf-8")) > 1_048_576:
        raise HTTPException(status_code=413, detail="content exceeds 1 MiB")
    validate_metadata(body.metadata)

    async with tenant_connection(request, tenant_id) as connection:
        await connection.execute(
            """INSERT INTO agent_memory.sessions (tenant_id, session_id)
               VALUES ($1, $2) ON CONFLICT (tenant_id, session_id) DO NOTHING""",
            tenant_id,
            session_id,
        )
        await connection.fetchrow(
            """SELECT session_id FROM agent_memory.sessions
               WHERE tenant_id = $1 AND session_id = $2 FOR UPDATE""",
            tenant_id,
            session_id,
        )
        message_id = await connection.fetchval(
            """INSERT INTO agent_memory.messages
                   (tenant_id, session_id, role, content, metadata, idempotency_key)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (tenant_id, session_id, idempotency_key)
                   WHERE idempotency_key IS NOT NULL DO NOTHING
               RETURNING message_id""",
            tenant_id,
            session_id,
            body.role,
            body.content,
            body.metadata,
            body.idempotency_key,
        )
        if message_id is None:
            message_id = await connection.fetchval(
                """SELECT message_id FROM agent_memory.messages
                   WHERE tenant_id = $1 AND session_id = $2 AND idempotency_key = $3""",
                tenant_id,
                session_id,
                body.idempotency_key,
            )
        await connection.execute(
            "UPDATE agent_memory.sessions SET updated_at = now() WHERE tenant_id = $1 AND session_id = $2",
            tenant_id,
            session_id,
        )
    return {"message_id": message_id, "session_id": session_id}


@app.get("/v1/sessions/{session_id}/context")
async def get_context(
    session_id: str,
    request: Request,
    limit: int = 20,
    tenant_id: str = Depends(authenticated_tenant),
) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    validate_session_id(session_id)
    async with tenant_connection(request, tenant_id) as connection:
        row = await connection.fetchrow(
            """SELECT COALESCE(s.summary, '') AS summary,
                      COALESCE(m.items, '[]'::jsonb) AS messages,
                      COALESCE(s.compacted_through_id, 0) AS compacted_through_id
               FROM (SELECT 1) AS seed
               LEFT JOIN agent_memory.sessions s
                 ON s.tenant_id = $1 AND s.session_id = $2
               LEFT JOIN LATERAL (
                   SELECT jsonb_agg(
                       jsonb_build_object(
                           'message_id', recent.message_id,
                           'role', recent.role,
                           'content', recent.content,
                           'metadata', recent.metadata,
                           'created_at', recent.created_at
                       ) ORDER BY recent.message_id
                   ) AS items
                   FROM (
                       SELECT message_id, role, content, metadata, created_at
                       FROM agent_memory.messages
                       WHERE tenant_id = $1 AND session_id = $2
                       ORDER BY message_id DESC
                       LIMIT $3
                   ) recent
               ) m ON true""",
            tenant_id,
            session_id,
            limit,
        )
    return dict(row)


@app.post("/v1/sessions/{session_id}/compact")
async def compact_session(
    session_id: str,
    body: CompactSession,
    request: Request,
    tenant_id: str = Depends(authenticated_tenant),
) -> dict[str, Any]:
    validate_session_id(session_id)
    if len(body.summary.encode("utf-8")) > 1_048_576:
        raise HTTPException(status_code=413, detail="summary exceeds 1 MiB")
    async with tenant_connection(request, tenant_id) as connection:
        session = await connection.fetchrow(
            """SELECT compacted_through_id FROM agent_memory.sessions
               WHERE tenant_id = $1 AND session_id = $2 FOR UPDATE""",
            tenant_id,
            session_id,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        if body.through_message_id <= session["compacted_through_id"]:
            raise HTTPException(status_code=409, detail="compaction boundary is not newer")
        boundary_exists = await connection.fetchval(
            """SELECT EXISTS (SELECT 1 FROM agent_memory.messages
               WHERE tenant_id = $1 AND session_id = $2 AND message_id = $3)""",
            tenant_id,
            session_id,
            body.through_message_id,
        )
        if not boundary_exists:
            raise HTTPException(status_code=422, detail="boundary message does not exist in this session")
        deleted = await connection.fetchval(
            """WITH removed AS (
                   DELETE FROM agent_memory.messages
                   WHERE tenant_id = $1 AND session_id = $2 AND message_id <= $3
                   RETURNING 1
               ) SELECT count(*) FROM removed""",
            tenant_id,
            session_id,
            body.through_message_id,
        )
        await connection.execute(
            """UPDATE agent_memory.sessions
               SET summary = $3, compacted_through_id = $4, updated_at = now()
               WHERE tenant_id = $1 AND session_id = $2""",
            tenant_id,
            session_id,
            body.summary,
            body.through_message_id,
        )
    return {"deleted_messages": deleted, "compacted_through_id": body.through_message_id}


@app.put("/v1/memories", status_code=201)
async def upsert_memory(
    body: MemoryWrite,
    request: Request,
    tenant_id: str = Depends(authenticated_tenant),
) -> dict[str, str]:
    validate_metadata(body.metadata)
    if body.session_id is not None and (not body.session_id.strip() or len(body.session_id.encode("utf-8")) > 512):
        raise HTTPException(status_code=422, detail="invalid session_id")
    vector = validate_vector(body.vector) if body.vector is not None else await embed_text(request, body.text)
    memory_id = (
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant_id}:{body.idempotency_key}"))
        if body.idempotency_key
        else str(uuid.uuid4())
    )
    payload = {
        "tenant_id": tenant_id,
        "text": body.text,
        "metadata": body.metadata,
        "session_id": body.session_id,
    }
    await qdrant_request(
        request,
        "PUT",
        f"/collections/{QDRANT_COLLECTION}/points?wait=true",
        json={"points": [{"id": memory_id, "vector": vector, "payload": payload}]},
    )
    return {"memory_id": memory_id}


@app.post("/v1/memories/search")
async def search_memories(
    body: MemorySearch,
    request: Request,
    tenant_id: str = Depends(authenticated_tenant),
) -> dict[str, Any]:
    if body.session_id is not None and (not body.session_id.strip() or len(body.session_id.encode("utf-8")) > 512):
        raise HTTPException(status_code=422, detail="invalid session_id")
    vector = validate_vector(body.vector) if body.vector is not None else await embed_text(request, body.query or "")
    search: dict[str, Any] = {
        "vector": vector,
        "limit": body.limit,
        "with_payload": True,
        "filter": tenant_filter(tenant_id, body.session_id),
    }
    if body.score_threshold is not None:
        search["score_threshold"] = body.score_threshold
    result = await qdrant_request(
        request,
        "POST",
        f"/collections/{QDRANT_COLLECTION}/points/search",
        json=search,
    )
    matches = [
        {
            "memory_id": point["id"],
            "score": point["score"],
            "text": point["payload"].get("text", ""),
            "metadata": point["payload"].get("metadata", {}),
            "session_id": point["payload"].get("session_id"),
        }
        for point in result.get("result", [])
    ]
    return {"matches": matches}


@app.delete("/v1/memories/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: uuid.UUID,
    request: Request,
    tenant_id: str = Depends(authenticated_tenant),
) -> Response:
    await qdrant_request(
        request,
        "POST",
        f"/collections/{QDRANT_COLLECTION}/points/delete?wait=true",
        json={"filter": {"must": tenant_filter(tenant_id)["must"] + [{"has_id": [str(memory_id)]}]}},
    )
    return Response(status_code=204)


@app.delete("/v1/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    request: Request,
    tenant_id: str = Depends(authenticated_tenant),
) -> Response:
    validate_session_id(session_id)
    await qdrant_request(
        request,
        "POST",
        f"/collections/{QDRANT_COLLECTION}/points/delete?wait=true",
        json={"filter": {"must": tenant_filter(tenant_id, session_id)["must"]}},
    )
    async with tenant_connection(request, tenant_id) as connection:
        await connection.execute(
            "DELETE FROM agent_memory.sessions WHERE tenant_id = $1 AND session_id = $2",
            tenant_id,
            session_id,
        )
    return Response(status_code=204)