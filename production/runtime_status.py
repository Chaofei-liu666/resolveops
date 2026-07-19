"""Runtime readiness and operational status helpers."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, text

from .config import settings
from .migrations import applied_versions, migration_files
from .models import Task


def expected_migration_versions() -> list[str]:
    return [path.name.split('_', 1)[0] for path in migration_files()]


def _task_counts(db: Any) -> dict[str, int]:
    rows = db.execute(select(Task.status, func.count()).group_by(Task.status)).all()
    return {str(status): int(count) for status, count in rows}


def build_runtime_status(db: Any) -> dict[str, Any]:
    """Return non-secret operational status for readiness and admin diagnostics."""
    db.execute(text('SELECT 1'))
    expected = expected_migration_versions()
    applied = sorted(applied_versions(db))
    pending = [version for version in expected if version not in applied]
    tasks_by_status = _task_counts(db)
    checks = {
        'database': {'ok': True},
        'migrations': {
            'ok': not pending,
            'expected_versions': expected,
            'applied_versions': applied,
            'pending_versions': pending,
        },
        'configuration': {
            'erpnext_configured': bool(settings.erpnext_base_url and settings.erpnext_api_key and settings.erpnext_api_secret),
            'llm_configured': bool(settings.llm_base_url and settings.llm_api_key and settings.llm_model),
            'webhook_secret_configured': bool(settings.webhook_secret),
            'operator_bootstrap_configured': bool(settings.operator_api_key),
            'operator_seed_keys_enabled': bool(settings.operator_seed_keys),
        },
    }
    ready = checks['database']['ok'] and checks['migrations']['ok']
    return {
        'status': 'ready' if ready else 'degraded',
        'checks': checks,
        'queues': {
            'tasks_by_status': tasks_by_status,
            'queued': tasks_by_status.get('queued', 0),
            'running': tasks_by_status.get('running', 0),
            'failed': tasks_by_status.get('failed', 0),
        },
    }
