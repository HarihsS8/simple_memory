CREATE SCHEMA IF NOT EXISTS agent_memory;

CREATE TABLE IF NOT EXISTS agent_memory.tenants (
    tenant_id text PRIMARY KEY CHECK (
        octet_length(tenant_id) BETWEEN 1 AND 128
        AND length(btrim(tenant_id)) > 0
    ),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_memory.api_keys (
    key_id uuid PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES agent_memory.tenants(tenant_id) ON DELETE CASCADE,
    token_hash char(64) NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    label text NOT NULL CHECK (octet_length(label) BETWEEN 1 AND 128),
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

CREATE TABLE IF NOT EXISTS agent_memory.sessions (
    tenant_id text NOT NULL REFERENCES agent_memory.tenants(tenant_id) ON DELETE CASCADE,
    session_id text NOT NULL CHECK (
        octet_length(session_id) BETWEEN 1 AND 512
        AND length(btrim(session_id)) > 0
    ),
    summary text NOT NULL DEFAULT '' CHECK (octet_length(summary) <= 1048576),
    compacted_through_id bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, session_id)
);

CREATE TABLE IF NOT EXISTS agent_memory.messages (
    message_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id text NOT NULL,
    session_id text NOT NULL,
    role text NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content text NOT NULL CHECK (octet_length(content) <= 1048576),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (octet_length(metadata::text) <= 65536),
    idempotency_key text CHECK (idempotency_key IS NULL OR octet_length(idempotency_key) BETWEEN 1 AND 128),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, session_id)
        REFERENCES agent_memory.sessions(tenant_id, session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS agent_messages_tenant_session_id_idx
    ON agent_memory.messages (tenant_id, session_id, message_id DESC);

CREATE UNIQUE INDEX IF NOT EXISTS agent_messages_idempotency_idx
    ON agent_memory.messages (tenant_id, session_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

ALTER TABLE agent_memory.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_memory.sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_memory.messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_memory.messages FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sessions_tenant_isolation ON agent_memory.sessions;
CREATE POLICY sessions_tenant_isolation ON agent_memory.sessions
    USING (tenant_id = current_setting('agent_memory.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('agent_memory.tenant_id', true));

DROP POLICY IF EXISTS messages_tenant_isolation ON agent_memory.messages;
CREATE POLICY messages_tenant_isolation ON agent_memory.messages
    USING (tenant_id = current_setting('agent_memory.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('agent_memory.tenant_id', true));

REVOKE ALL ON SCHEMA agent_memory FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA agent_memory FROM PUBLIC;
GRANT USAGE ON SCHEMA agent_memory TO memory_app;
GRANT SELECT, INSERT, UPDATE ON agent_memory.tenants, agent_memory.api_keys TO memory_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON agent_memory.sessions, agent_memory.messages TO memory_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA agent_memory TO memory_app;