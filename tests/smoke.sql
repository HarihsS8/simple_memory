BEGIN;

DO $$
DECLARE
    v_session_id text := 'smoke-' || pg_backend_pid()::text;
    v_old_id bigint;
    v_new_id bigint;
    v_context record;
    v_removed integer;
BEGIN
    v_old_id := chat_memory.append_message(v_session_id, 'user', 'older turn');
    v_new_id := chat_memory.append_message(v_session_id, 'assistant', 'newer turn');

    SELECT * INTO v_context FROM chat_memory.get_context(v_session_id, 20);
    IF jsonb_array_length(v_context.messages) <> 2 THEN
        RAISE EXCEPTION 'context should contain both messages';
    END IF;

    v_removed := chat_memory.compact(v_session_id, 'older turn summarized', v_old_id);
    IF v_removed <> 1 THEN
        RAISE EXCEPTION 'expected one deleted message, got %', v_removed;
    END IF;

    SELECT * INTO v_context FROM chat_memory.get_context(v_session_id, 20);
    IF v_context.summary <> 'older turn summarized'
       OR jsonb_array_length(v_context.messages) <> 1
       OR (v_context.messages->0->>'id')::bigint <> v_new_id THEN
        RAISE EXCEPTION 'compaction did not preserve the summary and newer message';
    END IF;
END;
$$;

ROLLBACK;