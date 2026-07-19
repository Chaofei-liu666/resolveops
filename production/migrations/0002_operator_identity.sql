CREATE TABLE IF NOT EXISTS operators (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR(80) NOT NULL DEFAULT 'demo',
    subject VARCHAR(140) NOT NULL,
    role VARCHAR(80) NOT NULL,
    api_key_hash VARCHAR(64) NOT NULL UNIQUE,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_operators_tenant_id ON operators (tenant_id);
CREATE INDEX IF NOT EXISTS ix_operators_subject ON operators (subject);
CREATE INDEX IF NOT EXISTS ix_operators_role ON operators (role);
CREATE INDEX IF NOT EXISTS ix_operators_status ON operators (status);
