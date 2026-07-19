ALTER TABLE approvals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS revoked_by VARCHAR(140);
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS revocation_reason TEXT;

CREATE INDEX IF NOT EXISTS ix_approvals_expires_at ON approvals (expires_at);
