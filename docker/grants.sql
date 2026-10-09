REVOKE ALL ON SCHEMA chat_memory FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA chat_memory FROM PUBLIC, memory_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA chat_memory FROM PUBLIC, memory_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA chat_memory FROM PUBLIC;
REVOKE ALL ON SCHEMA chat_memory FROM memory_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA chat_memory FROM memory_app;

GRANT USAGE ON SCHEMA agent_memory TO memory_app;
GRANT SELECT, INSERT, UPDATE ON agent_memory.tenants, agent_memory.api_keys TO memory_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON agent_memory.sessions, agent_memory.messages TO memory_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA agent_memory TO memory_app;