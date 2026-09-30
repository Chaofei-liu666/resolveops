CREATE TABLE IF NOT EXISTS chat_sessions (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(80) NOT NULL,
    operator_subject VARCHAR(140) NOT NULL,
    title VARCHAR(160) NOT NULL,
    summary TEXT NULL,
    memory_saved_at TIMESTAMP NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_tenant_id ON chat_sessions (tenant_id);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_operator_subject ON chat_sessions (operator_subject);

CREATE TABLE IF NOT EXISTS chat_messages (
    id VARCHAR(64) PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    route VARCHAR(40) NULL,
    "references" JSON DEFAULT '{}',
    attachments JSON DEFAULT '[]',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_chat_messages_session_id ON chat_messages (session_id);

CREATE TABLE IF NOT EXISTS chat_memories (
    id VARCHAR(64) PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL UNIQUE,
    tenant_id VARCHAR(80) NOT NULL,
    operator_subject VARCHAR(140) NOT NULL,
    title VARCHAR(160) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_chat_memories_tenant_id ON chat_memories (tenant_id);
CREATE INDEX IF NOT EXISTS ix_chat_memories_operator_subject ON chat_memories (operator_subject);
