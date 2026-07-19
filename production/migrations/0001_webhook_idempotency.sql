-- Run once during deployment if application startup migrations are disabled.
ALTER TABLE cases ADD COLUMN IF NOT EXISTS source_event_id VARCHAR(160);
ALTER TABLE cases ADD COLUMN IF NOT EXISTS event_type VARCHAR(80) NOT NULL DEFAULT 'inventory_shortage';
CREATE UNIQUE INDEX IF NOT EXISTS uq_cases_tenant_source_event
ON cases (tenant_id, source_event_id)
WHERE source_event_id IS NOT NULL;

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_error TEXT;

ALTER TABLE approvals ADD COLUMN IF NOT EXISTS required_roles JSON NOT NULL DEFAULT '["warehouse_manager"]'::json;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS approved_roles JSON NOT NULL DEFAULT '[]'::json;
