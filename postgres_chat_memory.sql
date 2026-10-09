CREATE SCHEMA IF NOT EXISTS chat_memory;

CREATE TABLE IF NOT EXISTS chat_memory.conversations (
    session_id text PRIMARY KEY CONSTRAINT conversations_session_id_size_check
        CHECK (octet_length(session_id) BETWEEN 1 AND 512 AND length(btrim(session_id)) > 0),
    summary text NOT NULL DEFAULT '' CONSTRAINT conversations_summary_size_check
        CHECK (octet_length(summary) <= 1048576),
    compacted_through_id bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_memory.messages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id text NOT NULL REFERENCES chat_memory.conversations(session_id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content text NOT NULL CONSTRAINT messages_content_size_check
        CHECK (octet_length(content) <= 1048576),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CONSTRAINT messages_metadata_size_check
        CHECK (octet_length(metadata::text) <= 65536),
    created_at timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'chat_memory.conversations'::regclass
          AND conname = 'conversations_session_id_size_check'
    ) THEN
        ALTER TABLE chat_memory.conversations
            ADD CONSTRAINT conversations_session_id_size_check
            CHECK (octet_length(session_id) BETWEEN 1 AND 512 AND length(btrim(session_id)) > 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'chat_memory.conversations'::regclass
          AND conname = 'conversations_summary_size_check'
    ) THEN
        ALTER TABLE chat_memory.conversations
            ADD CONSTRAINT conversations_summary_size_check
            CHECK (octet_length(summary) <= 1048576);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'chat_memory.messages'::regclass
          AND conname = 'messages_content_size_check'
    ) THEN
        ALTER TABLE chat_memory.messages
            ADD CONSTRAINT messages_content_size_check
            CHECK (octet_length(content) <= 1048576);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'chat_memory.messages'::regclass
          AND conname = 'messages_metadata_size_check'
    ) THEN
        ALTER TABLE chat_memory.messages
            ADD CONSTRAINT messages_metadata_size_check
            CHECK (octet_length(metadata::text) <= 65536);
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS messages_session_id_id_idx
    ON chat_memory.messages (session_id, id);

CREATE OR REPLACE FUNCTION chat_memory.append_message(
    p_session_id text,
    p_role text,
    p_content text,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, chat_memory
AS $$
DECLARE
    v_message_id bigint;
BEGIN
    IF p_session_id IS NULL OR length(btrim(p_session_id)) = 0 THEN
        RAISE EXCEPTION 'session_id must not be empty';
    END IF;

    IF p_role IS NULL OR p_role NOT IN ('system', 'user', 'assistant', 'tool') THEN
        RAISE EXCEPTION 'role must be system, user, assistant, or tool';
    END IF;

    INSERT INTO chat_memory.conversations (session_id)
    VALUES (p_session_id)
    ON CONFLICT (session_id) DO NOTHING;

    PERFORM 1
    FROM chat_memory.conversations
    WHERE session_id = p_session_id
    FOR UPDATE;

    INSERT INTO chat_memory.messages (session_id, role, content, metadata)
    VALUES (p_session_id, p_role, p_content, COALESCE(p_metadata, '{}'::jsonb))
    RETURNING id INTO v_message_id;

    UPDATE chat_memory.conversations
    SET updated_at = now()
    WHERE session_id = p_session_id;

    RETURN v_message_id;
END;
$$;

CREATE OR REPLACE FUNCTION chat_memory.get_context(
    p_session_id text,
    p_message_limit integer DEFAULT 20
)
RETURNS TABLE (
    summary text,
    messages jsonb,
    compacted_through_id bigint
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, chat_memory
AS $$
    SELECT
        COALESCE(c.summary, ''),
        COALESCE(
            jsonb_agg(
                jsonb_build_object(
                    'id', recent.id,
                    'role', recent.role,
                    'content', recent.content,
                    'metadata', recent.metadata,
                    'created_at', recent.created_at
                ) ORDER BY recent.id
            ) FILTER (WHERE recent.id IS NOT NULL),
            '[]'::jsonb
        ),
        COALESCE(c.compacted_through_id, 0)
    FROM (SELECT p_session_id AS session_id) requested
    LEFT JOIN chat_memory.conversations c ON c.session_id = requested.session_id
    LEFT JOIN LATERAL (
        SELECT selected.*
        FROM (
            SELECT m.*
            FROM chat_memory.messages m
            WHERE m.session_id = c.session_id
            ORDER BY m.id DESC
            LIMIT GREATEST(1, LEAST(COALESCE(p_message_limit, 20), 100))
        ) selected
        ORDER BY selected.id
    ) recent ON true
    GROUP BY c.summary, c.compacted_through_id;
$$;

CREATE OR REPLACE FUNCTION chat_memory.compact(
    p_session_id text,
    p_summary text,
    p_through_message_id bigint
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, chat_memory
AS $$
DECLARE
    v_compacted_through_id bigint;
    v_deleted_count integer;
BEGIN
    IF p_summary IS NULL THEN
        RAISE EXCEPTION 'summary must not be null';
    END IF;

    SELECT compacted_through_id
    INTO v_compacted_through_id
    FROM chat_memory.conversations
    WHERE session_id = p_session_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown session_id: %', p_session_id;
    END IF;

    IF p_through_message_id IS NULL OR p_through_message_id <= v_compacted_through_id THEN
        RAISE EXCEPTION 'through_message_id must be newer than the current compaction boundary';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM chat_memory.messages
        WHERE session_id = p_session_id AND id = p_through_message_id
    ) THEN
        RAISE EXCEPTION 'through_message_id does not belong to this session or no longer exists';
    END IF;

    UPDATE chat_memory.conversations
    SET summary = p_summary,
        compacted_through_id = p_through_message_id,
        updated_at = now()
    WHERE session_id = p_session_id;

    DELETE FROM chat_memory.messages
    WHERE session_id = p_session_id AND id <= p_through_message_id;

    GET DIAGNOSTICS v_deleted_count = ROW_COUNT;
    RETURN v_deleted_count;
END;
$$;

REVOKE ALL ON SCHEMA chat_memory FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA chat_memory FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA chat_memory FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA chat_memory FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA chat_memory
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;