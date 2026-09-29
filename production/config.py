"""Runtime configuration with a safe, local UI-editable connection profile.

Environment variables remain the deployment source of truth.  The optional
runtime profile is deliberately limited to connection settings a local
``config_admin`` may update from the Workbench. It lives in the local runtime
profile, never returns secrets, and is not a substitute for production secret
management.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    app_env: str = 'local'
    # SQLite is the zero-service default for desktop and source development.
    # A service deployment may override this with a PostgreSQL URL.
    database_url: str = 'sqlite:///./data/resolveops.db'
    erpnext_base_url: str
    erpnext_api_key: str
    erpnext_api_secret: str
    webhook_secret: str
    operator_api_key: str
    operator_seed_keys: str | None = None
    alternative_warehouses: str = '重庆仓,上海仓'
    task_lease_seconds: int = 90
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = 60
    llm_max_retries: int = 1
    agent_max_investigation_turns: int = 8
    agent_max_read_tool_calls: int = 12
    agent_read_tool_parallelism: int = 4
    agent_max_plan_repairs: int = 1
    agent_max_replans: int = 2
    approval_ttl_seconds: int = 3600
    enable_fault_injection: bool = False
    erpnext_company: str | None = None
    erpnext_stock_difference_account: str | None = None
    erpnext_default_valuation_rate: float = 100
    local_file_read_enabled: bool = False


settings = Settings()

# These are connection values that are useful for a local Workbench.  Runtime
# limits, operator bootstrap keys, webhook secrets and safety switches are
# intentionally not editable through the operator UI.
CONNECTION_FIELDS = {
    'llm_base_url', 'llm_api_key', 'llm_model', 'llm_timeout_seconds',
    'erpnext_base_url', 'erpnext_api_key', 'erpnext_api_secret',
    'database_url',
}
RUNTIME_MUTABLE_CONNECTION_FIELDS = CONNECTION_FIELDS - {'database_url'}
SECRET_CONNECTION_FIELDS = {'llm_api_key', 'erpnext_api_key', 'erpnext_api_secret'}


def _runtime_config_path() -> Path:
    configured = os.getenv('RESOLVEOPS_RUNTIME_CONFIG_PATH')
    if configured:
        return Path(configured)
    return Path.cwd() / '.resolveops-runtime' / 'connections.json'


def _read_runtime_profile() -> dict[str, Any]:
    path = _runtime_config_path()
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    values = payload.get('values') if isinstance(payload, dict) else None
    return values if isinstance(values, dict) else {}


def _write_runtime_profile(profile: dict[str, Any]) -> None:
    path = _runtime_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(
        json.dumps({'version': 1, 'updated_at': datetime.now(UTC).isoformat(), 'values': profile}, ensure_ascii=False),
        encoding='utf-8',
    )
    temporary.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _apply_values(values: dict[str, Any], *, include_database: bool) -> set[str]:
    applied: set[str] = set()
    for name, value in values.items():
        if name not in CONNECTION_FIELDS or (name == 'database_url' and not include_database):
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if getattr(settings, name) != value:
            setattr(settings, name, value)
            applied.add(name)
    return applied


def reload_runtime_connections() -> set[str]:
    """Refresh mutable LLM/ERP settings in a long-running API or Worker.

    A SQLAlchemy engine cannot safely change database endpoints in-process;
    database changes are loaded only when the service starts again.
    """
    return _apply_values(_read_runtime_profile(), include_database=False)


def save_runtime_connections(updates: dict[str, Any]) -> tuple[set[str], bool]:
    """Persist a partial local connection profile and apply safe live values."""
    accepted = {
        name: value for name, value in updates.items()
        if name in CONNECTION_FIELDS and value is not None and not (isinstance(value, str) and not value.strip())
    }
    profile = _read_runtime_profile()
    profile.update(accepted)
    _write_runtime_profile(profile)
    _apply_values(accepted, include_database=False)
    return set(accepted), 'database_url' in accepted


def connection_value(name: str) -> Any:
    if name not in CONNECTION_FIELDS:
        raise KeyError(name)
    return getattr(settings, name)


def secret_configured(name: str) -> bool:
    value = connection_value(name)
    return isinstance(value, str) and bool(value.strip()) and value.strip().lower() not in {'replace-me', 'changeme'}


def save_local_file_read_enabled(enabled: bool) -> bool:
    """Persist the desktop user's one-time choice for the read-only file tool."""
    profile = _read_runtime_profile()
    profile['local_file_read_enabled'] = bool(enabled)
    _write_runtime_profile(profile)
    settings.local_file_read_enabled = bool(enabled)
    return settings.local_file_read_enabled


# Apply a saved profile during API/Worker boot, including an explicitly chosen
# database endpoint.  The latter is never live-swapped after process start.
_apply_values(_read_runtime_profile(), include_database=True)
if isinstance(_read_runtime_profile().get('local_file_read_enabled'), bool):
    settings.local_file_read_enabled = _read_runtime_profile()['local_file_read_enabled']
