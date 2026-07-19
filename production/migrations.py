"""Versioned SQL migration runner.

This is intentionally small: ResolveOps already keeps SQL files under
production/migrations, so the production-oriented next step is to make those
files applied in order and recorded in schema_migrations.  The application still
uses SQLAlchemy metadata to create an empty development database, but schema
evolution is tracked through migration versions.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import text

MIGRATIONS_DIR = Path(__file__).resolve().parent / 'migrations'


def migration_files() -> list[Path]:
    return sorted(path for path in MIGRATIONS_DIR.glob('*.sql') if path.name[:4].isdigit())


def ensure_schema_migrations_table(connection: Any) -> None:
    connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version VARCHAR(40) PRIMARY KEY,
            filename TEXT NOT NULL,
            checksum VARCHAR(64) NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    ))


def applied_versions(connection: Any) -> set[str]:
    ensure_schema_migrations_table(connection)
    rows = connection.execute(text('SELECT version FROM schema_migrations')).all()
    return {row[0] for row in rows}


def apply_migrations(connection: Any) -> list[dict[str, str]]:
    """Apply unapplied SQL migrations inside the caller's transaction."""
    use_postgres_lock = connection.dialect.name == 'postgresql'
    if use_postgres_lock:
        connection.execute(text("SELECT pg_advisory_lock(hashtext('resolveops_schema_migrations'))"))
    applied: list[dict[str, str]] = []
    try:
        ensure_schema_migrations_table(connection)
        seen = applied_versions(connection)
        for path in migration_files():
            version = path.name.split('_', 1)[0]
            sql = path.read_text(encoding='utf-8')
            checksum = hashlib.sha256(sql.encode('utf-8')).hexdigest()
            if version in seen:
                continue
            connection.execute(text(sql))
            connection.execute(
                text(
                    'INSERT INTO schema_migrations(version, filename, checksum) '
                    'VALUES (:version, :filename, :checksum)'
                ),
                {'version': version, 'filename': path.name, 'checksum': checksum},
            )
            applied.append({'version': version, 'filename': path.name, 'checksum': checksum})
    finally:
        if use_postgres_lock:
            connection.execute(text("SELECT pg_advisory_unlock(hashtext('resolveops_schema_migrations'))"))
    return applied
