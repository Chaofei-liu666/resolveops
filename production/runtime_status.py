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


def _is_placeholder(value: str | None) -> bool:
    if not value:
        return True
    normalized = value.strip().lower()
    return (
        normalized in {'replace-me', 'changeme', 'change-me', 'example', 'test'}
        or normalized.startswith('replace-')
        or 'api.example.com' in normalized
    )


def _has_required_secret(value: str | None) -> bool:
    return not _is_placeholder(value)


def _configuration_check() -> dict[str, Any]:
    env = (settings.app_env or 'local').strip().lower()
    production_like = env in {'staging', 'production'}
    erpnext_configured = (
        _has_required_secret(settings.erpnext_base_url)
        and _has_required_secret(settings.erpnext_api_key)
        and _has_required_secret(settings.erpnext_api_secret)
    )
    llm_configured = (
        _has_required_secret(settings.llm_base_url)
        and _has_required_secret(settings.llm_api_key)
        and _has_required_secret(settings.llm_model)
    )
    webhook_secret_configured = _has_required_secret(settings.webhook_secret)
    operator_bootstrap_configured = _has_required_secret(settings.operator_api_key)
    operator_seed_keys_enabled = bool(settings.operator_seed_keys)
    errors: list[str] = []
    warnings: list[str] = []

    if production_like:
        if not erpnext_configured:
            errors.append('erpnext_credentials_missing_or_placeholder')
        if not llm_configured:
            errors.append('llm_credentials_required_for_production_like_env')
        if not webhook_secret_configured:
            errors.append('webhook_secret_missing_or_placeholder')
        if not operator_bootstrap_configured:
            errors.append('operator_api_key_missing_or_placeholder')
        if operator_seed_keys_enabled:
            errors.append('operator_seed_keys_not_allowed_in_production_like_env')
    else:
        if not llm_configured:
            warnings.append('llm_not_configured_agent_may_use_deterministic_fallback')
        if operator_seed_keys_enabled:
            warnings.append('operator_seed_keys_enabled_for_local_development')

    return {
        'ok': not errors,
        'app_env': env,
        'production_like': production_like,
        'erpnext_configured': erpnext_configured,
        'llm_configured': llm_configured,
        'webhook_secret_configured': webhook_secret_configured,
        'operator_bootstrap_configured': operator_bootstrap_configured,
        'operator_seed_keys_enabled': operator_seed_keys_enabled,
        'errors': errors,
        'warnings': warnings,
    }


def build_runtime_status(db: Any) -> dict[str, Any]:
    """Return non-secret operational status for readiness and admin diagnostics."""
    db.execute(text('SELECT 1'))
    expected = expected_migration_versions()
    applied = sorted(applied_versions(db))
    pending = [version for version in expected if version not in applied]
    tasks_by_status = _task_counts(db)
    configuration = _configuration_check()
    checks = {
        'database': {'ok': True},
        'migrations': {
            'ok': not pending,
            'expected_versions': expected,
            'applied_versions': applied,
            'pending_versions': pending,
        },
        'configuration': configuration,
    }
    ready = checks['database']['ok'] and checks['migrations']['ok'] and checks['configuration']['ok']
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
