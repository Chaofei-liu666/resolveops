# ResolveOps schema migrations

SQL files in this directory are applied in filename order and recorded in
`schema_migrations`.

The application still creates missing base tables in local development with
SQLAlchemy metadata, but schema evolution must be represented by versioned SQL
files here.

For production, run the same migrations through CI/CD with a database migration
user, then remove DDL rights from the API and Worker roles.

`0001_webhook_idempotency.sql` covers the first online schema evolution:
webhook idempotency, event type routing, worker lease recovery fields, and
multi-role approvals.
