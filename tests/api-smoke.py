import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV = {
    key: value
    for line in (ROOT / ".env").read_text().splitlines()
    if line and not line.startswith("#") and "=" in line
    for key, value in [line.split("=", 1)]
}
BASE_URL = f"http://127.0.0.1:{ENV.get('MEMORY_API_PORT', '8080')}"
ADMIN_HEADERS = {"X-Admin-Token": ENV["MEMORY_ADMIN_TOKEN"]}
KEY_IDS: list[str] = []


def call(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    token: str | None = None,
    admin: bool = False,
    expected_status: int = 200,
) -> dict | None:
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if admin:
        headers.update(ADMIN_HEADERS)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
            result = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        result = error.read()
    if status != expected_status:
        raise AssertionError(f"{method} {path}: expected {expected_status}, got {status}: {result[:500]!r}")
    return json.loads(result) if result else None


def create_key(tenant_id: str) -> str:
    result = call(
        "POST",
        "/admin/v1/api-keys",
        body={"tenant_id": tenant_id, "label": "api-smoke"},
        admin=True,
        expected_status=201,
    )
    KEY_IDS.append(result["key_id"])
    return result["api_key"]


def main() -> None:
    tenant_a = f"smoke-a-{uuid.uuid4().hex}"
    tenant_b = f"smoke-b-{uuid.uuid4().hex}"
    session_id = f"session-{uuid.uuid4().hex}"
    query = urllib.parse.quote(session_id, safe="")
    key_a = create_key(tenant_a)
    key_b = create_key(tenant_b)

    call("GET", "/v1/sessions/not-authenticated/context", expected_status=401)
    call(
        "POST",
        f"/v1/sessions/{query}/messages",
        body={"tenant_id": tenant_b, "role": "user", "content": "spoof tenant"},
        token=key_a,
        expected_status=422,
    )

    first = call(
        "POST",
        f"/v1/sessions/{query}/messages",
        body={"role": "user", "content": "remember this turn", "idempotency_key": "turn-1"},
        token=key_a,
        expected_status=201,
    )
    retry = call(
        "POST",
        f"/v1/sessions/{query}/messages",
        body={"role": "user", "content": "remember this turn", "idempotency_key": "turn-1"},
        token=key_a,
        expected_status=201,
    )
    assert first["message_id"] == retry["message_id"], "idempotent retry inserted a duplicate"

    context_a = call("GET", f"/v1/sessions/{query}/context", token=key_a)
    context_b = call("GET", f"/v1/sessions/{query}/context", token=key_b)
    assert len(context_a["messages"]) == 1 and not context_b["messages"], "session isolation failed"

    call(
        "POST",
        f"/v1/sessions/{query}/compact",
        body={
            "summary": "The user asked the agent to remember this turn.",
            "through_message_id": first["message_id"],
        },
        token=key_a,
    )
    compacted = call("GET", f"/v1/sessions/{query}/context", token=key_a)
    assert compacted["summary"] and not compacted["messages"], "session compaction failed"

    vector = [1.0] + [0.0] * (int(ENV.get("VECTOR_SIZE", "1536")) - 1)
    memory = call(
        "PUT",
        "/v1/memories",
        body={
            "text": "tenant A private project detail",
            "metadata": {"category": "test"},
            "session_id": session_id,
            "idempotency_key": "memory-1",
            "vector": vector,
        },
        token=key_a,
        expected_status=201,
    )
    search_body = {"vector": vector, "session_id": session_id, "limit": 5}
    results_a = call("POST", "/v1/memories/search", body=search_body, token=key_a)
    results_b = call("POST", "/v1/memories/search", body=search_body, token=key_b)
    assert len(results_a["matches"]) == 1, "tenant A could not retrieve its memory"
    assert not results_b["matches"], "tenant B retrieved tenant A's memory"

    call("DELETE", f"/v1/memories/{memory['memory_id']}", token=key_b, expected_status=204)
    still_visible = call("POST", "/v1/memories/search", body=search_body, token=key_a)
    assert len(still_visible["matches"]) == 1, "tenant B deleted tenant A's memory"

    call("DELETE", f"/v1/sessions/{query}", token=key_a, expected_status=204)
    after_delete = call("POST", "/v1/memories/search", body=search_body, token=key_a)
    assert not after_delete["matches"], "session deletion left linked vector memories behind"

    key_id = KEY_IDS.pop(0)
    call("DELETE", f"/admin/v1/api-keys/{key_id}", admin=True, expected_status=204)
    call("GET", f"/v1/sessions/{query}/context", token=key_a, expected_status=401)

    print("Agent memory API passed auth, tenant isolation, idempotency, compaction, RAG, and deletion checks.")


if __name__ == "__main__":
    try:
        main()
    finally:
        for key_id in KEY_IDS:
            try:
                call("DELETE", f"/admin/v1/api-keys/{key_id}", admin=True, expected_status=204)
            except Exception as error:
                print(f"Warning: unable to revoke smoke-test key {key_id}: {error}", file=os.sys.stderr)